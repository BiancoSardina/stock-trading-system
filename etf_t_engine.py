#!/usr/bin/env python3
"""ETF日内做T引擎 (etf_t_engine.py) V2.0
====================================================
设计文档：ETF 日内做T交易模块设计文档 V1.0 + V1.1 优化方向（用户 2026-08-10）
模块定位：只对【持仓ETF】做日内 T 增强——高抛低吸降成本。
  V1.1 核心升级（用户拍板，防止"正确判断、错误执行"）：
    ① 交易时间窗口状态机（OPENING/T1/T_MAIN/NOON/T2/LAST/NO_SELL_T）
    ② T机会生命周期（30分钟降评分 / 60分钟失效 / 14:00后自动失效）
    ③ 卖T必须绑定回补路径（T_profit≥min_profit 才允许；14:00后窗口需>1.5%）
    ④ 主升保护 TREND_RUN（连续3日上涨/放量/破20日新高，满足2条禁卖T）
    ⑤ 日内强弱位置（(cur-low)/(high-low)：<30%禁卖，>80%高位优卖）
    ⑥ ETF专属指标：流动性(成交额)过滤 + 板块同步提示（IOPV尽力而为）
    ⑦ ALLOW_T 综合闸门：时间窗口×流动性×趋势×T空间×日内位置，任一=0 禁止
  V2.0 低吸T模块（用户 2026-08-11 拍板，只对 ETF 开放）：
    正T吃上涨波动（卖高买低），低吸T吃下跌波动（买低卖高，T+1 隔日卖）。
    ① 独立状态机 DIP_NORMAL→READY_BUY_DIP→HOLD_T_BUY→SELL_T→DIP_NORMAL
    ② 低吸评分五因子：下跌空间25+趋势保护25+恐慌释放20+止跌确认20+板块环境10，≥70 执行
    ③ ALLOW_BUY_DIP 闸门：时间窗口×流动性×趋势×放量破位×板块退潮×资金预留×持仓×额度
    ④ 补强三点（评审确认）：正T待回补资金冲突禁低吸；低吸份额冻结(正T可卖额度=底仓−冻结)；
       恐慌释放(缩量企稳)与放量破位(资金撤退)量化区分
    ⑤ T+1 卖出：盈利≥1.5%卖50% / ≥3%全卖 / 突破昨高卖 / 高开>1.5%兑现 / 止损-3%只退T仓
    ⑥ 买入价=MIN(MA5, 日内低×1.005)；单次≤min(T仓×50%, 总仓×10%)；14:00后禁低吸

核心思想（T 不预测方向）：卖出高位波动 → 等待回落 → 买回相同数量 → 降低成本。
"没有回补窗口，不允许产生卖出信号"——这是 ETF_T 与趋势交易最大的区别。
A股 ETF 为 T+1：T 卖出的是可卖老仓，当天买回的新仓次日才能再卖。
低吸T 买入当天同样不可卖，T+1 起执行止盈/止损/趋势恢复卖出。
"""
import json
import os
import sys
import csv
from datetime import datetime, date

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import position_manager
from runtime import data_path, atomic_json, read_json, exclusive, available_quantity, lot_quantity, positive, analysis_only, quote_is_fresh

STATE_FILE = data_path("t_state.json")
T_LOG = data_path("t_trade_log.csv")

# ── T状态机 ──
NORMAL = "NORMAL"
READY_SELL = "READY_SELL"
SELL_DONE = "SELL_DONE"
WAIT_BUYBACK = "WAIT_BUYBACK"
COMPLETE = "COMPLETE"

# ── V2.0 低吸T状态机 ──
DIP_NORMAL = "DIP_NORMAL"
READY_BUY_DIP = "READY_BUY_DIP"
HOLD_T_BUY = "HOLD_T_BUY"
SELL_T = "SELL_T"

# ── 参数 ──
MIN_LIQUIDITY = 30_000_000        # 日成交额门槛 3000万（盘中按进度折算）
T_LIFE_MIN = 60                   # T机会生命周期（分钟）
T_LIFE_WARN = 30                  # 30分钟未成交 → 降评分警告
LAST_WINDOW_SPACE = 0.015         # 14:00-14:30 最后窗口要求回补空间>1.5%

# ── V2.0 低吸T参数 ──
DIP_SCORE_MIN = 70                # 低吸评分门槛
DIP_BUDGET_RATIO = 0.5            # 低吸T最大使用 T仓×50%
DIP_MAX_AMOUNT_RATIO = 0.10       # 单次买入 ≤ 总仓×10%
DIP_TP1 = 0.015                   # 第一目标 +1.5% 卖50%
DIP_TP2 = 0.03                    # 第二目标 +3% 全卖
DIP_SL = 0.03                     # 止损 -3%（只退T仓，不卖核心仓）
DIP_GAP_SELL = 0.015              # T+1 高开>1.5% 直接兑现（恐慌修复）
DIP_PANIC_DROP = 0.07             # 跌幅超过-7%需额外确认（止跌确认必须强）

# 关联指数映射（板块同步判断；查不到跳过不阻塞）
INDEX_MAP = {
    "159516": [("sz399006", "创业板指"), ("sh000688", "科创50")],
    "562800": [("sh000016", "上证50"), ("sz399006", "创业板指")],
    "159858": [("sz399006", "创业板指"), ("sh000688", "科创50")],
}


# ────────────────────── 数据获取 ──────────────────────

def fetch_quote(code):
    """新浪实时行情 → dict；失败返回 None"""
    try:
        import urllib.request
        pref = "sh" if code.startswith(("5", "6", "9")) else "sz"
        url = f"https://hq.sinajs.cn/list={pref}{code}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", "ignore")
        body = raw.split('"')[1]
        f = body.split(",")
        if len(f) < 32 or not f[0]:
            return None
        iopv = None
        try:  # 部分ETF行情尾部带 IOPV/估值字段（尽力而为）
            if len(f) > 32 and f[32]:
                iopv = float(f[32])
        except Exception:
            pass
        return {
            "name": f[0], "open": float(f[1]), "prev_close": float(f[2]),
            "cur": float(f[3]), "high": float(f[4]), "low": float(f[5]),
            "volume": float(f[8]), "amount": float(f[9]),
            "date": f[30], "time": f[31] if len(f) > 31 else f[30],
            "iopv": iopv,
        }
    except Exception:
        return None


def _kline(code, days=120):
    """日K（新浪）；5/6/9 开头=沪市，1/0 开头=深市"""
    try:
        import json as _json
        import urllib.request as _ur
        pref = "sh" if code.startswith(("5", "6", "9")) else "sz"
        url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"CN_MarketData.getKLineData?symbol={pref}{code}&scale=240&ma=5&datalen={days}")
        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
        return _json.loads(_ur.urlopen(req, timeout=10).read().decode("gbk"))
    except Exception:
        return []


def _kline5(code, days=96):
    """5分钟K（新浪）；V2.0 低吸T 恐慌释放/止跌确认用；失败返回空列表"""
    try:
        import json as _json
        import urllib.request as _ur
        pref = "sh" if code.startswith(("5", "6", "9")) else "sz"
        url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"CN_MarketData.getKLineData?symbol={pref}{code}&scale=5&ma=5&datalen={days}")
        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
        return _json.loads(_ur.urlopen(req, timeout=10).read().decode("gbk"))
    except Exception:
        return []


def _idx_kline(symbol, days=10):
    """指数日K（symbol 为完整形式如 sh000016）；板块退潮判断用；失败返回空列表"""
    try:
        import json as _json
        import urllib.request as _ur
        url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=5&datalen={days}")
        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
        return _json.loads(_ur.urlopen(req, timeout=10).read().decode("gbk"))
    except Exception:
        return []


def _idx_quotes(code):
    """关联指数实时行情 → [(name, 涨跌幅%, 从日内低点回升?)]；失败返回空列表"""
    rows = []
    for sym, name in INDEX_MAP.get(code, []):
        try:
            import urllib.request
            url = f"https://hq.sinajs.cn/list={sym}"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
            raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "ignore")
            f = raw.split('"')[1].split(",")
            if len(f) > 6 and f[0]:
                pc = float(f[2]); cur = float(f[3]); low = float(f[5])
                pct = (cur - pc) / pc * 100 if pc else 0
                rebound = low > 0 and cur > low * 1.005
                rows.append((name, pct, rebound))
        except Exception:
            pass
    return rows


def _sector_3day_down(code):
    """板块退潮：关联指数任一连续3日收跌 → True（尽力而为，查不到=False）"""
    for sym, _name in INDEX_MAP.get(code, []):
        k = _idx_kline(sym, 10)
        if len(k) >= 4:
            closes = [float(x["close"]) for x in k]
            if closes[-1] < closes[-2] < closes[-3]:
                return True
    return False


def _ma(closes, n):
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 4)


def _time_progress(tstr):
    """按当前时间折算盘中进度（9:30-11:30 / 13:00-15:00）"""
    try:
        hh, mm, ss = (int(x) for x in tstr.split(":"))
        t = hh * 60 + mm
        if 9 * 60 + 30 <= t <= 11 * 60 + 30:
            return (t - 9 * 60 - 30) / 120
        if 13 * 60 <= t <= 15 * 60:
            return 0.5 + (t - 13 * 60) / 120
        return 1.0
    except Exception:
        return 1.0


# ────────────────────── V1.1 ① 交易时间窗口状态机 ──────────────────────

def market_window(tstr):
    """按行情时间返回交易窗口 dict：{name, can_sell, can_buy, note}
    09:30-09:45 OPENING  开盘观察（禁卖禁买，噪声大）
    09:45-10:30 T1       第一T窗口（卖T需强势高开）
    10:30-11:15 T_MAIN   主要T窗口（最佳，卖+回补）
    11:15-13:30 NOON     午间观察（只允许回补，禁主动卖T）
    13:30-14:00 T2       第二T窗口（午后拉升可卖）
    14:00-14:30 LAST     最后卖T窗口（回补空间必须>1.5%）
    14:30+    NO_SELL_T  关闭卖T（防止裸仓过夜，持仓过夜等明日）
    """
    try:
        hh, mm = int(tstr[:2]), int(tstr[3:5])
        t = hh * 60 + mm
    except Exception:
        return {"name": "UNKNOWN", "can_sell": True, "can_buy": True, "note": "时间未知"}
    if t < 9 * 60 + 30:
        return {"name": "PRE", "can_sell": False, "can_buy": False, "note": "未开盘"}
    if t < 9 * 60 + 45:
        return {"name": "OPENING", "can_sell": False, "can_buy": False, "note": "开盘观察期(9:30-9:45)，噪声大禁T"}
    if t < 10 * 60 + 30:
        return {"name": "T1", "can_sell": True, "can_buy": True, "note": "第一T窗口，卖T需强势高开"}
    if t < 11 * 60 + 15:
        return {"name": "T_MAIN", "can_sell": True, "can_buy": True, "note": "主要T窗口(10:30-11:15)，机构冲高兑现阶段，最佳"}
    if t < 13 * 60 + 30:
        return {"name": "NOON", "can_sell": False, "can_buy": True, "note": "午间观察(11:15-13:30)，只允许等待回补"}
    if t < 14 * 60:
        return {"name": "T2", "can_sell": True, "can_buy": True, "note": "第二T窗口(13:30-14:00)，午后拉升可卖"}
    if t < 14 * 60 + 30:
        return {"name": "LAST", "can_sell": True, "can_buy": True, "note": "最后卖T窗口(14:00-14:30)，回补空间需>1.5%"}
    return {"name": "NO_SELL_T", "can_sell": False, "can_buy": True, "note": "14:30后关闭卖T，裸仓过夜风险大，持仓过夜等明日"}


# ────────────────────── V2.0 低吸T时间窗口 ──────────────────────

def dip_window(tstr):
    """低吸T买入窗口（V2.0 设计文档第六部分）：
    09:30-09:45 禁        开盘噪声
    09:45-10:30 观察窗口   只评分不买
    10:30-11:15 最佳低吸   允许买入
    11:15-13:30 谨慎       仅允许小仓
    13:30-14:00 二次低吸   允许买入
    14:00+      禁止       防尾盘接飞刀 + 资金留给正T LAST 回补
    返回 {name, can_buy, small, note}
    """
    try:
        hh, mm = int(tstr[:2]), int(tstr[3:5])
        t = hh * 60 + mm
    except Exception:
        return {"name": "DIP_UNKNOWN", "can_buy": False, "small": False, "note": "时间未知，禁低吸"}
    if t < 9 * 60 + 45:
        return {"name": "DIP_OPEN", "can_buy": False, "small": False, "note": "开盘噪声(9:30-9:45)，禁低吸"}
    if t < 10 * 60 + 30:
        return {"name": "DIP_WATCH", "can_buy": False, "small": False, "note": "观察窗口(9:45-10:30)，只评分不买"}
    if t < 11 * 60 + 15:
        return {"name": "DIP_BEST", "can_buy": True, "small": False, "note": "最佳低吸(10:30-11:15)"}
    if t < 13 * 60 + 30:
        return {"name": "DIP_CAUT", "can_buy": True, "small": True, "note": "午间(11:15-13:30)，仅允许小仓"}
    if t < 14 * 60:
        return {"name": "DIP_SECOND", "can_buy": True, "small": False, "note": "二次低吸(13:30-14:00)"}
    return {"name": "DIP_OFF", "can_buy": False, "small": False, "note": "14:00后禁低吸，防尾盘接飞刀"}


# ────────────────────── V1.1 ⑤ 日内强弱位置 ──────────────────────

def day_position(q):
    """日内位置 (当前价-最低)/(最高-最低)；>80%高位 30%以下低位"""
    if q.get("high", 0) <= q.get("low", 0):
        return 1.0
    return (q["cur"] - q["low"]) / (q["high"] - q["low"])


# ────────────────────── V1.1 ④ 主升保护 TREND_RUN ──────────────────────

def trend_run_check(q, kline):
    """主升保护：连续3日上涨 / 成交量连续放大 / 突破20日新高，满足≥2条 → 禁止卖T
    返回 (is_trend_run, note)"""
    if len(kline) < 25:
        return False, ""
    closes = [float(x["close"]) for x in kline]
    vols = [float(x["volume"]) for x in kline]
    up3 = closes[-1] > closes[-2] > closes[-3]
    vol_up = vols[-1] > vols[-2] > vols[-3] and vols[-1] > sum(vols[-21:-1]) / 20
    high20 = max([float(x["high"]) for x in kline[-20:]])
    new20 = q["cur"] >= high20
    hits = sum([up3, vol_up, new20])
    if hits >= 2:
        return True, f"TREND_RUN主升保护(3日连涨/放量/破20日新高 满足{hits}条)，禁止卖T防卖飞"
    return False, f"非主升(命中{hits}/3)"


# ────────────────────── V1.1 ⑥ ETF专属指标 ──────────────────────

def liquidity_check(q):
    """流动性：日成交额（按时间进度折算）≥3000万"""
    progress = _time_progress(q.get("time", ""))
    amt = q.get("amount", 0) / progress if progress > 0 else q.get("amount", 0)
    return amt >= MIN_LIQUIDITY, amt


def sector_sync(q, code):
    """板块同步：ETF 与关联指数同涨同跌=强；背离=提示。返回 (同步?, note)"""
    try:
        import urllib.request
        idxs = INDEX_MAP.get(code, [])
        if not idxs:
            return None, ""
        etf_pct = (q["cur"] - q["prev_close"]) / q["prev_close"] * 100 if q["prev_close"] else 0
        rows = []
        for sym, name in idxs:
            url = f"https://hq.sinajs.cn/list={sym}"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
            raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "ignore")
            f = raw.split('"')[1].split(",")
            if len(f) > 3 and f[0]:
                pc = float(f[2])
                pct = (float(f[3]) - pc) / pc * 100 if pc else 0
                rows.append((name, pct))
        if not rows:
            return None, ""
        same = all((r[1] > 0) == (etf_pct > 0) or abs(r[1]) < 0.3 for r in rows)
        note = "板块同步:" + "/".join(f"{n}{p:+.1f}%" for n, p in rows)
        return same, note
    except Exception:
        return None, ""


# ────────────────────── V1.1 ③ 回补路径（卖T必须绑定买回路径）──────────────────────

def calc_buyback_path(q, kline, sell_price, min_profit):
    """计算预计回补价 + T利润空间。
    回补路径来自MA5/日内低点，收益要求不得用来制造目标价。
    返回 (est_buyback, t_profit)；无路径（空间<min_profit）返回 (None, 0)"""
    if not sell_price:
        return None, 0
    closes = [float(x["close"]) for x in kline]
    ma5 = _ma(closes, 5)
    cands = []
    if ma5:
        cands.append(ma5)
    if q.get("low", 0) > 0:
        cands.append(q["low"])
    cands = [x for x in cands if positive(x) and x < sell_price]
    if not cands:
        return None, 0
    est = max(cands)  # 最近的可观测支撑，收益要求只作过滤
    if (sell_price - est) / sell_price < min_profit:      # 支撑贴卖价 → 没有回补路径
        return None, 0
    t_profit = (sell_price - est) / sell_price
    return round(est, 3), round(t_profit, 4)


# ────────────────────── T 评分（V1.0 第六部分）──────────────────────

def calc_t_score(q, kline):
    """T评分 0-100：高开20 + 涨幅20 + 日内冲高15 + 回落确认15 + 量能15 + 趋势保护15
    V2.1：冲高回落30 → 日内冲高15+回落确认15（修复"冲高回落两头凑不满70"盲区）
    返回 (score, parts, meta)"""
    parts = {}
    pc = q["prev_close"]
    if pc <= 0:
        return 0, {}, {}
    gap = (q["open"] - pc) / pc * 100
    cur_pct = (q["cur"] - pc) / pc * 100
    high_pct = (q["high"] - pc) / pc * 100
    pullback = high_pct - cur_pct

    if gap < 1:
        parts["高开"] = 0
    elif gap < 2:
        parts["高开"] = 10
    elif gap <= 4:
        parts["高开"] = 20
    else:
        parts["高开"] = 5
    # V2.1 涨幅档位下调：做T卖点不需大涨，涨幅≥0.5%即有价值（配合形态确认）
    if cur_pct < 0.5:
        parts["涨幅"] = 0
    elif cur_pct < 2:
        parts["涨幅"] = 15
    elif cur_pct <= 4:
        parts["涨幅"] = 20
    else:
        parts["涨幅"] = 10
    # V2.1 冲高回落30 → 日内冲高15 + 回落确认15
    # 修复盲区：原"冲高回落"因子只在"已回落"时得分——冲高瞬间=0、回落之后涨幅=0，
    # "冲高3%回落收平"行情两头都凑不满70，信号永远出不来（2026-08-11 医药板块实锤）
    if high_pct < 1:
        parts["日内冲高"] = 0
    elif high_pct < 2:
        parts["日内冲高"] = 5
    elif high_pct < 3:
        parts["日内冲高"] = 10
    else:
        parts["日内冲高"] = 15
    if pullback < 0.8:
        parts["回落确认"] = 0
    else:
        parts["回落确认"] = 15
    closes = [float(x["close"]) for x in kline]
    vols = [float(x["volume"]) for x in kline]
    avg_vol = sum(vols[-20:]) / 20 if len(vols) >= 20 else (sum(vols) / len(vols) if vols else 0)
    progress = _time_progress(q.get("time", ""))
    cur_vol = q["volume"] / progress if progress > 0 else q["volume"]
    vol_ratio = cur_vol / avg_vol if avg_vol else 0
    if vol_ratio < 1:
        parts["量能"] = 0
    elif vol_ratio <= 2:
        parts["量能"] = 8
    else:
        parts["量能"] = 15
    tp = 0
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    if ma20 and q["cur"] > ma20:
        tp += 5
    if ma20 and len(closes) >= 25 and closes[-5] < closes[-1]:
        tp += 5
    high60 = max([float(x["high"]) for x in kline[-60:]]) if len(kline) >= 60 else None
    if high60 and q["cur"] >= high60 * 0.995:
        tp += 5
    parts["趋势保护"] = tp

    score = sum(parts.values())
    meta = {"gap": round(gap, 2), "cur_pct": round(cur_pct, 2),
            "high_pct": round(high_pct, 2), "pullback": round(pullback, 2),
            "vol_ratio": round(vol_ratio, 2), "ma20": ma20, "ma60": ma60}
    return score, parts, meta


# ────────────────────── 回补评分（V1.0 第八部分）──────────────────────

def calc_buyback_score(q, kline, sell_price):
    """BUYBACK_SCORE 0-100：跌幅30 + 靠近MA5 20 + 缩量20 + 分时止跌30；≥70 执行回补"""
    if not sell_price:
        return 0, {}
    parts = {}
    drop = (sell_price - q["cur"]) / sell_price * 100
    closes = [float(x["close"]) for x in kline]
    ma5 = _ma(closes, 5)
    vols = [float(x["volume"]) for x in kline]
    avg_vol = sum(vols[-20:]) / 20 if len(vols) >= 20 else (sum(vols) / len(vols) if vols else 0)
    progress = _time_progress(q.get("time", ""))
    cur_vol = q["volume"] / progress if progress > 0 else q["volume"]
    vol_ratio = cur_vol / avg_vol if avg_vol else 0

    parts["跌幅"] = 30 if q["cur"] <= sell_price * 0.98 else 0
    parts["靠近MA5"] = 20 if (ma5 and q["cur"] <= ma5 * 1.01) else 0
    parts["缩量"] = 20 if vol_ratio < 0.8 else 0
    parts["分时止跌"] = 30 if q["cur"] > q["low"] * 1.002 else 0

    return sum(parts.values()), parts


# ────────────────────── V1.1 ⑦ ALLOW_T 综合闸门 ──────────────────────

def allow_t_checks(q, kline, win, sell_price, min_profit, code):
    """ALLOW_T = 时间窗口 × 流动性 × 趋势状态 × T空间 × 日内位置；任一失败 → 禁止
    返回 (ok, checks, buyback_est, t_profit)"""
    checks = []

    # 1) 时间窗口
    checks.append({"k": "时间窗口", "ok": win["can_sell"], "note": f"{win['name']} {win['note']}"})

    # 2) 流动性
    liq_ok, amt = liquidity_check(q)
    checks.append({"k": "流动性", "ok": liq_ok, "note": f"成交额{amt/1e8:.2f}亿(门槛{MIN_LIQUIDITY/1e8:.2f}亿)"})

    # 3) 趋势状态（TREND_RUN 禁卖）
    trend_ok, trend_note = trend_run_check(q, kline)
    checks.append({"k": "趋势状态", "ok": not trend_ok, "note": trend_note})

    # 4) T空间（回补路径，核心）
    buyback_est, t_profit = calc_buyback_path(q, kline, sell_price, min_profit)
    need = LAST_WINDOW_SPACE if win["name"] == "LAST" else min_profit
    space_ok = buyback_est is not None and t_profit >= need
    checks.append({"k": "T空间", "ok": space_ok,
                   "note": f"回补路径≈{buyback_est} T利润{t_profit*100:.1f}%(需≥{need*100:.1f}%)" if buyback_est
                           else "无回补路径(支撑贴卖价)，禁止卖T"})

    # 5) 日内位置
    pos = day_position(q)
    pos_ok = pos >= 0.30
    checks.append({"k": "日内位置", "ok": pos_ok, "note": f"位置{pos*100:.0f}%(<30%低位禁卖,>80%高位优)"})

    ok = all(c["ok"] for c in checks)
    return ok, checks, buyback_est, t_profit


# ────────────────────── V2.0 低吸T评分（设计文档第七部分）──────────────────────

def calc_dip_score(q, kline, m5, code=""):
    """低吸T评分 0-100：下跌空间25 + 趋势保护25 + 恐慌释放20 + 止跌确认20 + 板块环境10
    返回 (score, parts, meta)；≥70 才执行"""
    parts = {}
    pc = q["prev_close"]
    if pc <= 0:
        return 0, {}, {}
    m5 = m5 or []
    cur_pct = (q["cur"] - pc) / pc * 100
    # 1) 下跌空间 25（今日跌幅）
    if cur_pct >= -1:
        parts["下跌空间"] = 0
    elif cur_pct >= -2:
        parts["下跌空间"] = int((cur_pct + 2) * 10)          # -1%~-2% → 0~10
    elif cur_pct >= -3:
        parts["下跌空间"] = 10 + int((cur_pct + 2) * 10)     # -2%~-3% → 10~20
    elif cur_pct >= -5:
        parts["下跌空间"] = 20 + int((cur_pct + 3) / 2 * 5)  # -3%~-5% → 20~25
    else:
        parts["下跌空间"] = 25
    # 2) 趋势保护 25
    closes = [float(x["close"]) for x in kline]
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    tp = 0
    if ma20 and q["cur"] > ma20:
        tp += 10
    if ma20 and len(closes) >= 25 and closes[-5] < closes[-1]:
        tp += 10
    if ma60 and q["cur"] > ma60:
        tp += 5
    parts["趋势保护"] = tp
    # 3) 恐慌释放 20（5分钟K：先放量杀跌 → 后缩量企稳）
    pan = 0
    if len(m5) >= 12:
        vols = [float(x["volume"]) for x in m5]
        base = sum(vols[-12:-2]) / 10 if len(vols) >= 12 else (sum(vols) / len(vols) if vols else 0)
        vol_surge = False
        vol_shrink = False
        if base > 0:
            # 放量杀跌：近10根内 量>基准×1.5 且收<开
            for k in m5[-10:]:
                if float(k["volume"]) > base * 1.5 and float(k["close"]) < float(k["open"]):
                    vol_surge = True
                    break
            # 缩量企稳：最近3根量 < 基准×0.8
            if max(float(x["volume"]) for x in m5[-3:]) < base * 0.8:
                vol_shrink = True
        if vol_surge:
            pan += 10
        if vol_shrink:
            pan += 10
    parts["恐慌释放"] = pan
    # 4) 止跌确认 20（两条可叠加）
    stb = 0
    if q["low"] > 0 and q["cur"] > q["low"] * 1.01:
        stb += 10
    if len(m5) >= 3 and float(m5[-1]["close"]) > float(m5[-2]["close"]) \
            and float(m5[-2]["close"]) > float(m5[-3]["close"]):
        stb += 10
    parts["止跌确认"] = stb
    # 5) 板块环境 10（关联指数：板块深跌+5 / 板块回升+5）
    sec = 0
    rows = _idx_quotes(code)
    for _n, pct, rebound in rows:
        if pct < -3:
            sec = max(sec, 5)
        if rebound:
            sec = max(sec, 5)
    if len(rows) >= 2 and all(r[1] < -3 for r in rows) and all(r[2] for r in rows):
        sec = 10
    parts["板块环境"] = sec

    score = sum(parts.values())
    # 超过-7%需额外确认：止跌确认未拿满20 → 扣10（恐慌极值要求强止跌）
    if cur_pct <= -DIP_PANIC_DROP * 100 and parts.get("止跌确认", 0) < 20:
        score -= 10
    meta = {"cur_pct": round(cur_pct, 2), "ma20": ma20, "ma60": ma60,
            "idx": [{"name": n, "pct": round(p, 2), "rebound": r} for n, p, r in rows]}
    return score, parts, meta


def calc_dip_buy_price(q, kline):
    """低吸买入价 = MIN(MA5, 日内低点×1.005)；返回 (buy_price, 买入区)"""
    closes = [float(x["close"]) for x in kline]
    ma5 = _ma(closes, 5)
    cands = []
    if ma5:
        cands.append(ma5)
    if q.get("low", 0) > 0:
        cands.append(q["low"] * 1.005)
    if not cands:
        return None, ""
    bp = min(cands)
    if q.get("low", 0) > 0:
        zone = f"{min(q['low'], bp):.3f}-{max(q['low'], bp):.3f}"   # 有序区间：低点~目标价
    else:
        zone = f"{bp:.3f}附近"
    return round(bp, 3), zone


def _dip_buy_shares(q, t_position, amount, price):
    """低吸买入份额 = min(T仓×50%, 总仓×10%) / 买入价，取整百；不足一手返回0"""
    budget = min(t_position * DIP_BUDGET_RATIO, amount * DIP_MAX_AMOUNT_RATIO)
    return max(int(budget / price / 100) * 100, 0)


# ────────────────────── V2.0 ALLOW_BUY_DIP 综合闸门 ──────────────────────

def allow_dip_checks(q, kline, dwin, code, st, t_position, amount):
    """ALLOW_BUY_DIP = 时间窗口×流动性×趋势状态×放量破位×板块退潮×资金预留×低吸持仓×仓位限制
    任一失败 → 禁止。返回 (ok, checks)"""
    checks = []
    # 1) 时间窗口
    checks.append({"k": "时间窗口", "ok": dwin["can_buy"], "note": f"{dwin['name']} {dwin['note']}"})
    # 2) 流动性
    liq_ok, amt = liquidity_check(q)
    checks.append({"k": "流动性", "ok": liq_ok, "note": f"成交额{amt/1e8:.2f}亿(门槛{MIN_LIQUIDITY/1e8:.2f}亿)"})
    # 3) 趋势状态：价<MA60 且 MA60 下行 → 禁（补强：低吸T不是套牢票自救工具）
    closes = [float(x["close"]) for x in kline]
    ma60 = _ma(closes, 60)
    ma60_prev = _ma(closes[:-1], 60) if len(closes) > 61 else None
    trend_ok = not (ma60 and ma60_prev and q["cur"] < ma60 and ma60 <= ma60_prev)
    checks.append({"k": "趋势状态", "ok": trend_ok,
                   "note": f"MA60={ma60}" + (" 趋势破坏(价<MA60且MA60下行)，禁低吸" if not trend_ok else " 趋势健康")})
    # 4) 放量破位：今日跌≥5% 且 量>5日均量×2 → 资金撤退禁吸（与恐慌释放量化区分）
    vols = [float(x["volume"]) for x in kline]
    avg5 = sum(vols[-5:]) / 5 if len(vols) >= 5 else 0
    progress = _time_progress(q.get("time", ""))
    cur_vol = q["volume"] / progress if progress > 0 else q["volume"]
    cur_pct = (q["cur"] - q["prev_close"]) / q["prev_close"] * 100 if q["prev_close"] else 0
    brk = cur_pct <= -5 and avg5 > 0 and cur_vol > avg5 * 2
    checks.append({"k": "放量破位", "ok": not brk,
                   "note": f"今日{cur_pct:.1f}% 量/5日均={cur_vol/avg5:.1f}倍" + ("，持续放量=资金撤退，禁吸" if brk else "")})
    # 5) 板块退潮：关联指数连续3日跌 → 禁
    sec3 = _sector_3day_down(code)
    checks.append({"k": "板块退潮", "ok": not sec3,
                   "note": "关联指数3日连跌，板块退潮禁吸" if sec3 else "板块未连续3日跌"})
    # 6) 资金预留：正T待回补期间禁低吸（补强1：回补资金优先）
    t_conflict = st.get("state") in (SELL_DONE, WAIT_BUYBACK)
    checks.append({"k": "资金预留", "ok": not t_conflict,
                   "note": "正T回补未完成，资金优先回补，禁低吸" if t_conflict else "正T无待回补，资金可用"})
    # 7) 低吸持仓：已有低吸仓待卖 → 禁重复低吸
    dip_st = st.get("dip") or {}
    dip_hold = dip_st.get("state") in (HOLD_T_BUY, SELL_T)
    checks.append({"k": "低吸持仓", "ok": not dip_hold,
                   "note": f"已有低吸仓{dip_st.get('buy_shares', 0)}份待卖，禁重复低吸" if dip_hold else "无低吸持仓"})
    # 8) 仓位限制：额度 ≥ 1手
    budget = min(t_position * DIP_BUDGET_RATIO, amount * DIP_MAX_AMOUNT_RATIO)
    one_lot = q["cur"] * 100
    checks.append({"k": "仓位限制", "ok": budget >= one_lot,
                   "note": f"低吸额度{budget:.0f}元(≥1手{one_lot:.0f}元)"})
    ok = all(c["ok"] for c in checks)
    return ok, checks


def _default_dip():
    """低吸T默认状态"""
    return {
        "state": DIP_NORMAL, "score": 0,
        "buy_price": None, "buy_shares": 0, "buy_time": "", "buy_date": "",
        "stop_loss": None, "target1": None, "target2": None,
        "yesterday_high": None, "sell_trigger": "",
        "last_reason": "", "date": "",
    }


# ────────────────────── 状态持久化 ──────────────────────

def load_t_state():
    value = read_json(STATE_FILE, {})
    if not isinstance(value, dict):
        raise ValueError("T状态数据格式错误")
    return value


def save_t_state(st):
    if not analysis_only():
        if _FILL_TX is not None:
            _FILL_TX["state"] = st
        else:
            atomic_json(STATE_FILE, st)


_FILL_TX = None


def fill_transaction(fn):
    """Commit fill facts and resulting T state in the same atomic JSON write."""
    from functools import wraps

    @exclusive(lambda: STATE_FILE)
    @wraps(fn)
    def wrapped(*args, **kwargs):
        global _FILL_TX
        _FILL_TX = {"events": [], "state": None}
        try:
            result = fn(*args, **kwargs)
            state = _FILL_TX["state"]
            if state is None:
                raise RuntimeError("成交未产生可保存状态")
            state.setdefault("__fills__", []).extend(_FILL_TX["events"])
            atomic_json(STATE_FILE, state)
            return result
        finally:
            _FILL_TX = None
    return wrapped


def _default_state(code, name):
    return {
        "code": code, "name": name, "state": NORMAL,
        "sell_price": None, "sell_shares": 0, "sell_time": "",
        "buyback_price": None, "buyback_time": "",
        "t_score": 0, "date": date.today().isoformat(),
        "today_high_pct": 0.0, "last_reason": "",
        "t_created_at": "", "t_created_time": "",
        "pending_warn": "", "shape_trigger": False,
        "dip": _default_dip(),   # V2.0 低吸T状态
    }


# ────────────────────── 主入口 analyze（V1.1 重构）──────────────────────

@exclusive(lambda: STATE_FILE)
def analyze(code, name=None, now=None):
    """对单只持仓ETF做日内T分析，推进状态机，返回报告 dict。

    V1.1 决策链路：
      市场时间窗口 → T机会生命周期 → ALLOW_T(时间×流动性×趋势×T空间×日内位置)
      → 卖T信号(绑定回补路径) / 回补信号 / 观望
    """
    today = date.today().isoformat()
    pos = position_manager.aggregate_positions().get(code)
    if not pos:
        return {"code": code, "error": "非持仓标的，不做T分析"}
    name = name or pos.get("name", code)

    cfg = pos.get("t_config") or {}
    enable = cfg.get("enable", True)
    t_ratio = float(cfg.get("t_ratio", 0.2))
    min_profit = float(cfg.get("min_profit", 0.008))
    max_sell_ratio = float(cfg.get("max_sell_ratio", 0.5))
    amount = float(pos.get("amount") or 0)
    t_position = float(cfg.get("t_position") or amount * t_ratio)

    q = fetch_quote(code)
    if not q:
        return {"code": code, "name": name, "error": "行情获取失败"}
    if not quote_is_fresh(q, now) or not positive(q.get("cur")) or not positive(q.get("prev_close")):
        return {"code": code, "error": "报价过期或价格无效，保留未完成T状态"}
    kline = _kline(code, 120)
    if not kline:
        return {"code": code, "name": name, "error": "日K获取失败"}

    st = load_t_state().get(code) or _default_state(code, name)
    if st.get("date") != today:
        if st.get("state") in (SELL_DONE, WAIT_BUYBACK):
            st["pending_warn"] = f"昨日{st['state']}未完成（卖出{st.get('sell_price')}），T仓需人工核对"
        # V2.0：低吸T跨日持仓（HOLD_T_BUY/SELL_T）必须保留（T+1 未卖完），其余重置
        dip_keep = st.get("dip") or {}
        keep_dip = dip_keep.get("state") in (HOLD_T_BUY, SELL_T)
        if st.get("state") not in (SELL_DONE, WAIT_BUYBACK):
            st = _default_state(code, name)
        if keep_dip:
            st["dip"] = dip_keep
        st["date"] = today

    win = market_window(q.get("time", ""))
    cur_pct = (q["cur"] - q["prev_close"]) / q["prev_close"] * 100 if q["prev_close"] else 0
    high_pct = (q["high"] - q["prev_close"]) / q["prev_close"] * 100 if q["prev_close"] else 0
    st["today_high_pct"] = round(high_pct, 2)

    t_score, parts, meta = calc_t_score(q, kline)
    st["t_score"] = t_score

    sell_zone = buyback_zone = risk = ""
    reasons = []
    state = st["state"]

    def _sell_shares():
        # V2.0：正T可卖额度 = min(T仓, 总仓×max_sell_ratio) − 低吸T冻结份额
        cap = min(t_position, amount * max_sell_ratio)
        dip_st = st.get("dip") or {}
        frozen = dip_st.get("buy_shares", 0) if dip_st.get("state") in (HOLD_T_BUY, SELL_T) else 0
        available = available_quantity(pos, today)
        return max(int(min(cap / q["cur"], max(available - frozen, 0)) / 100) * 100, 0)

    # ── T机会生命周期检查（V1.1 ②）──
    life_note = ""
    if state == READY_SELL and st.get("t_created_time"):
        try:
            ch, cm = (int(x) for x in st["t_created_time"].split(":")[:2])
            qh, qm = (int(x) for x in q["time"].split(":")[:2])
            age = (qh * 60 + qm) - (ch * 60 + cm)
            if age > T_LIFE_MIN or (qh * 60 + qm) >= 14 * 60:
                st["state"] = NORMAL
                state = NORMAL
                life_note = f"T机会超生命周期({age}分钟/{T_LIFE_MIN}分钟)或已过14:00，自动失效"
            elif age > T_LIFE_WARN:
                life_note = f"T机会已{age}分钟未成交，评分降低，{T_LIFE_MIN - age}分钟后失效"
        except Exception:
            pass

    # ── 状态机推进 ──
    if state == NORMAL:
        # V2.2 当日冲高状态行（任何窗口都显示：让用户知道今天有T机会/已错过）
        if high_pct >= 2.5:
            reasons.append(f"📈 今日曾冲高{high_pct:.1f}%（现涨{cur_pct:.1f}%）")
        if not enable:
            action = "T已关闭(t_config.enable=false)"
            reasons.append("关闭状态")
        elif not win["can_sell"]:
            action = "观望"
            reasons.append(f"⛔ 时间窗口 {win['name']}：{win['note']}")
            if win["name"] == "NO_SELL_T":
                reasons.append("策略：持仓过夜，等待明日机会")
            elif win["name"] == "OPENING":
                reasons.append("开盘噪声大，9:45后再看T机会")
                # V2.2 早盘冲高预案：OPENING 禁卖是防噪声硬规则，但提前告知用户触发条件
                if high_pct >= 2.5 and cur_pct >= 0.5:
                    reasons.append(f"📌 早盘已冲高{high_pct:.1f}%（现涨{cur_pct:.1f}%）："
                                   f"9:45后若现价仍≥+0.5%将直接触发卖T信号，可提前挂单高点×0.995")
            elif win["name"] == "NOON":
                reasons.append("午间方向不明，只等回补不主动卖T")
        else:
            sh = _sell_shares()
            ok, checks, buyback_est, t_profit = allow_t_checks(
                q, kline, win, q["cur"], min_profit, code)
            for c in checks:
                reasons.append(f"{'✓' if c['ok'] else '✗'} {c['k']}: {c['note']}")
            # V2.2 冲高形态触发（2026-08-11 用户实锤修复，替代 V2.1 的"回落确认"版）：
            # 日内曾冲高≥2.5% 且 现价仍≥昨收+0.5% → 直接放行卖T。
            # 关键：不再等"回落≥0.8%"——10:05 报告时若刚冲高未回落（pullback<0.8），
            # V2.1 会漏信号，用户只能在回落后才看到提示（卖点已晚）。冲高时提示=卖在最高区。
            # 回补路径/流动性/趋势/日内位置 仍由 allow_t_checks 把关（V1.1 铁律不变）。
            # 单边主升卖飞风险由 TREND_RUN 闸门（连涨+放量+破新高≥2条禁卖）保护。
            pullback = meta.get("high_pct", 0) - meta.get("cur_pct", 0)
            shape_hit = (high_pct >= 2.5 and cur_pct >= 0.5)
            gap = meta.get("gap", 0)
            # T1 窗口常规需强势高开；形态触发豁免（冲高已确认，非开盘噪声）
            if win["name"] == "T1" and gap < 1 and not shape_hit:
                action = "观望"
                reasons.append(f"⛔ T1窗口需强势高开(高开{gap:.1f}%<1%)，暂不卖T")
            elif ok and sh >= 100 and (t_score >= 70 or shape_hit):
                st["state"] = READY_SELL
                st["sell_price"] = round(q["cur"], 3)
                st["sell_shares"] = sh
                st["sell_time"] = q.get("time", "")
                st["t_created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                st["t_created_time"] = q.get("time", "")
                state = READY_SELL
                action = f"允许卖T：卖出T仓{sh}份"
                if shape_hit and t_score < 70:
                    st["shape_trigger"] = True
                    st["last_reason"] = (f"冲高形态触发(冲高{high_pct:.1f}%现涨{cur_pct:.1f}%，"
                                         f"回补路径{t_profit*100:.1f}%)")
                    if cur_pct >= 1.5:
                        reasons.append(f"✓ 冲高形态触发：日内冲高{high_pct:.1f}%，现价仍涨{cur_pct:.1f}%"
                                       f"（回落初期），卖出区=现价附近直接高抛（回补路径≈{buyback_est}，"
                                       f"T利润{t_profit*100:.1f}%）")
                    else:
                        reasons.append(f"✓ 冲高形态触发：日内冲高{high_pct:.1f}%已回落{pullback:.1f}%，"
                                       f"现价{cur_pct:.1f}%（回落确认），等反弹至卖出区再卖"
                                       f"（回补路径≈{buyback_est}，T利润{t_profit*100:.1f}%）")
                else:
                    st["shape_trigger"] = False
                    st["last_reason"] = f"T评分{t_score}触发卖T(绑定回补路径{t_profit*100:.1f}%)"
                    reasons.append(f"✓ T评分 {t_score}/100 ≥70，信号成立")
            else:
                action = "观望"
                if shape_hit:
                    reasons.append(f"⚠️ 日内曾冲高{high_pct:.1f}%现涨{cur_pct:.1f}%（形态已现），"
                                   f"但闸门未全过或T仓不足，暂不卖T")
                elif t_score < 70:
                    reasons.append(f"✗ T评分 {t_score}/100 < 70")
                if sh < 100:
                    reasons.append("✗ T仓不足一手(<100份)")
    elif state == READY_SELL:
        ok, checks, _, _ = allow_t_checks(q, kline, win, q["cur"], min_profit, code)
        if not enable or not ok or _sell_shares() < 100 or t_score < 70:
            st["state"] = state = NORMAL
            action = "原卖T机会失效，暂停执行"
        else:
            action = "等待卖出"
        if life_note:
            reasons.append(f"⚠️ {life_note}")
        else:
            reasons.append("T信号成立，等待高抛执行；按回补路径挂回补单")
    elif state in (SELL_DONE, WAIT_BUYBACK):
        action = "等待回补"
        sell_price = st.get("sell_price")
        if sell_price:
            bs, bparts = calc_buyback_score(q, kline, float(sell_price))
            buyback_zone = f"{float(sell_price)*0.98:.3f}-{min(float(sell_price), (meta.get('ma20') or float(sell_price))):.3f}"
            if bs >= 70:
                action = "回补信号（可买回）"
                reasons.append(f"✓ 回补评分 {bs}/100（{'/'.join(f'{k}{v}' for k, v in bparts.items() if v)}）")
                st["state"] = WAIT_BUYBACK
            else:
                reasons.append(f"回补评分 {bs}/100 < 70，继续等待回落")
            if not win["can_sell"]:
                reasons.append(f"窗口 {win['name']}：{win['note']}")
    elif state == COMPLETE:
        action = "本轮T已完成"
        reasons.append("等待下次T机会")

    # 输出卖出区（READY_SELL / SELL_DONE）
    ref_sell = ref_buyback = ""
    # V2.2 错失机会复盘（用户纪律：前瞻+预案）：今日曾冲高≥2.5%但现价已回落到<0.5%（无肉可卖）
    if state == NORMAL and high_pct >= 2.5 and cur_pct < 0.5:
        reasons.append(f"⚠️ 今日曾冲高{high_pct:.1f}%已回落{high_pct-cur_pct:.1f}%，卖点错过；"
                       f"明日预案：再冲高≥2.5%时立即执行卖T（不等回落，回落初期直接高抛）")
    if state in (READY_SELL, SELL_DONE) and st.get("sell_price"):
        sp = float(st["sell_price"])
        sh = st.get("sell_shares") or _sell_shares()
        est, tp = calc_buyback_path(q, kline, sp, min_profit)
        # V2.2 形态触发且已回落较深(cur<1.5%)：卖出区=等反弹（现价×1.005~高点×0.995），
        # 避免用户在回落低位割肉；回落初期/评分触发则按现价~+1%直接卖
        if st.get("shape_trigger") and cur_pct < 1.5 and q["high"] > q["cur"]:
            sell_zone = f"{q['cur']*1.005:.3f}-{q['high']*0.995:.3f}"
        else:
            sell_zone = f"{sp:.3f}-{sp*1.01:.3f}"
        buyback_zone = f"{est:.3f}附近" if est else f"{sp*0.98:.3f}附近"
        risk = _risk_text(q, meta, st)
    else:
        risk = _risk_text(q, meta, st)
        # V2.0：观望态也给预案参考点位（用户纪律：观望必须带触发价）
        closes = [float(x["close"]) for x in kline]
        ma5 = _ma(closes, 5)
        if ma5 and q["low"] > 0 and q["low"] < ma5:
            ref_buyback = f"{q['low']:.3f}-{ma5:.3f}"          # 回补参考：日内低~MA5
        elif ma5:
            ref_buyback = f"{ma5:.3f}附近"
        elif q["low"] > 0:
            ref_buyback = f"{q['low']:.3f}附近"
        hi = q["high"]
        if hi > q["cur"] * 1.005:                               # 日内高点未触及 → 再冲高可卖
            ref_sell = f"{hi*0.995:.3f}-{hi:.3f}"
        else:                                                   # 已在最高位 → 上方小空间即卖
            ref_sell = f"{q['cur']*1.005:.3f}-{q['cur']*1.02:.3f}"

    # ── V2.0 低吸T分析（只对 ETF 开放；用户 2026-08-11 拍板）──
    dip_rep = None
    if pos.get("type") == "etf":
        dip_rep = analyze_dip(code, name, q, kline, st, t_position, amount)

    # 板块同步提示（不阻塞）
    try:
        sync, sync_note = sector_sync(q, code)
        if sync_note:
            reasons.append(f"{'✓' if sync else '⚠️'} {sync_note}")
    except Exception:
        pass

    st["date"] = today
    st["last_reason"] = st.get("last_reason") or (reasons[-1] if reasons else action)
    _all = load_t_state()
    _all[code] = st
    save_t_state(_all)

    rep = {
        "code": code, "name": name, "state": st["state"],
        "window": win["name"], "window_note": win["note"],
        "today_high_pct": round(high_pct, 2), "cur_pct": round(cur_pct, 2),
        "t_score": t_score, "action": action,
        "sell_zone": sell_zone, "buyback_zone": buyback_zone,
        "risk": risk, "reasons": reasons,
        "t_position": round(t_position, 0), "shares": max(0, int(st.get("sell_shares") or 0) - int(st.get("buyback_shares") or 0)),
        "pending_warn": st.get("pending_warn", ""),
        "parts": parts, "meta": meta,
        "cur": q["cur"], "time": q.get("time", ""),
        "day_pos": round(day_position(q) * 100, 0),
        "iopv": q.get("iopv"),
        "ref_sell_zone": ref_sell, "ref_buyback_zone": ref_buyback,
        "dip": dip_rep,
    }
    return rep


# ────────────────────── V2.0 低吸T主分析 ──────────────────────

def analyze_dip(code, name, q, kline, st, t_position, amount):
    """低吸T状态机推进：
    DIP_NORMAL →(评分≥70+闸门全过)→ READY_BUY_DIP →(用户买入)→ HOLD_T_BUY
    →(T+1 止盈/止损/突破昨高/高开兑现)→ SELL_T →(用户卖出)→ DIP_NORMAL
    返回报告 dict（含 dip 状态引用，调用方负责 save_t_state）"""
    today = date.today().isoformat()
    dip = st.setdefault("dip", _default_dip())
    dwin = dip_window(q.get("time", ""))
    m5 = _kline5(code, 96)
    score, parts, meta = calc_dip_score(q, kline, m5, code)
    dip["score"] = score
    state = dip["state"]
    action = "观望"
    reasons = []
    buy_zone = sell_zone = stop_zone = ref_buy_zone = ""
    # 预案参考低吸区（无论状态都计算，标注非信号）
    _rbp, _rbz = calc_dip_buy_price(q, kline)
    if _rbp:
        ref_buy_zone = _rbz

    if state == DIP_NORMAL:
        if dwin["can_buy"]:
            ok, checks = allow_dip_checks(q, kline, dwin, code, st, t_position, amount)
            for c in checks:
                reasons.append(f"{'✓' if c['ok'] else '✗'} {c['k']}: {c['note']}")
            if ok and score >= DIP_SCORE_MIN:
                bp, bz = calc_dip_buy_price(q, kline)
                sh = _dip_buy_shares(q, t_position, amount, bp or q["cur"])
                if bp and sh >= 100:
                    dip["state"] = READY_BUY_DIP
                    dip["buy_price"] = bp
                    dip["buy_shares"] = sh
                    dip["sell_trigger"] = ""
                    dip["last_reason"] = f"低吸评分{score}触发"
                    state = READY_BUY_DIP
                    action = f"低吸买入信号：{bz} 共{sh}份"
                    reasons.append(f"✓ 低吸评分 {score}/100 ≥{DIP_SCORE_MIN}")
                else:
                    reasons.append("✗ 低吸额度不足一手(<100份)" if sh < 100 else "✗ 无有效买入价")
            else:
                if score < DIP_SCORE_MIN:
                    reasons.append(f"✗ 低吸评分 {score}/100 < {DIP_SCORE_MIN}")
        else:
            reasons.append(f"⛔ 低吸窗口 {dwin['name']}：{dwin['note']}")
    elif state == READY_BUY_DIP:
        ok, _ = allow_dip_checks(q, kline, dwin, code, st, t_position, amount)
        if not dwin["can_buy"] or not ok or score < DIP_SCORE_MIN:
            dip.clear()
            dip.update(_default_dip())
            state = DIP_NORMAL
            action = "原低吸机会失效，暂停执行"
        else:
            action = "等待低吸买入"
        bp = dip.get("buy_price")
        if bp:
            buy_zone = f"{q['low']:.3f}-{bp:.3f}"
            reasons.append(f"低吸信号成立：买入区 {buy_zone}，{dip.get('buy_shares', 0)}份；成交后告知我记录")
    elif state == HOLD_T_BUY:
        buy_price = float(dip.get("buy_price") or 0)
        shares_h = int(dip.get("buy_shares") or 0)
        if buy_price <= 0 or shares_h <= 0:
            dip["state"] = DIP_NORMAL
            state = DIP_NORMAL
            reasons.append("低吸持仓数据异常，重置")
        else:
            pnl_pct = (q["cur"] - buy_price) / buy_price * 100
            stop = buy_price * (1 - DIP_SL)
            tp1 = buy_price * (1 + DIP_TP1)
            tp2 = buy_price * (1 + DIP_TP2)
            yh = dip.get("yesterday_high") or 0
            buy_date = dip.get("buy_date", "")
            if buy_date and buy_date < today:
                # T+1 可卖：止损 > 目标2 > 高开兑现 > 突破昨高 > 目标1半仓
                gap = (q["open"] - q["prev_close"]) / q["prev_close"] * 100 if q["prev_close"] else 0
                if q["cur"] <= stop:
                    dip["state"] = SELL_T
                    dip["sell_trigger"] = "止损"
                    state = SELL_T
                    action = f"触发止损：现价{q['cur']:.3f}≤止损{stop:.3f}(-{DIP_SL*100:.0f}%)，卖低吸仓{shares_h}份"
                elif pnl_pct >= DIP_TP2 * 100:
                    dip["state"] = SELL_T
                    dip["sell_trigger"] = "目标2全卖"
                    state = SELL_T
                    action = f"达标第二目标：盈利{pnl_pct:.1f}%≥{DIP_TP2*100:.0f}%，全卖{shares_h}份"
                elif gap > DIP_GAP_SELL * 100:
                    dip["state"] = SELL_T
                    dip["sell_trigger"] = "高开兑现"
                    state = SELL_T
                    action = f"高开{gap:.1f}%>{DIP_GAP_SELL*100:.1f}%，恐慌修复兑现，卖{shares_h}份"
                elif yh and q["cur"] > yh:
                    dip["state"] = SELL_T
                    dip["sell_trigger"] = "突破昨高"
                    state = SELL_T
                    action = f"突破昨日高点{yh:.3f}，趋势恢复，卖{shares_h}份"
                elif pnl_pct >= DIP_TP1 * 100 and not dip.get("target1_done"):
                    half = max(int(shares_h / 2 / 100) * 100, 100)
                    dip["state"] = SELL_T
                    dip["sell_trigger"] = "目标1半仓"
                    dip["target_sell_shares"] = half
                    state = SELL_T
                    action = f"达标第一目标：盈利{pnl_pct:.1f}%≥{DIP_TP1*100:.0f}%，卖50%({half}份)"
                else:
                    action = f"持有：盈利{pnl_pct:+.1f}%（目标1 {tp1:.3f}/目标2 {tp2:.3f}/止损 {stop:.3f}）"
            else:
                action = f"T+1持有中（买入当日不可卖，明日再看）：买入@{buy_price:.3f} 盈利{pnl_pct:+.1f}%"
            sell_zone = f"{tp1:.3f}-{tp2:.3f}"
            stop_zone = f"{stop:.3f}"
    elif state == SELL_T:
        action = "等待低吸卖出"
        reasons.append(f"卖出触发：{dip.get('sell_trigger', '')}，{dip.get('target_sell_shares', dip.get('buy_shares', 0))}份待卖；成交后告知我记录")

    dip["date"] = today
    dip["last_reason"] = dip.get("last_reason") or (reasons[-1] if reasons else action)
    return {
        "state": state, "action": action, "score": score, "parts": parts,
        "buy_zone": buy_zone, "sell_zone": sell_zone, "stop_zone": stop_zone,
        "ref_buy_zone": ref_buy_zone,
        "shares": int(dip.get("buy_shares") or 0),
        "buy_price": dip.get("buy_price"), "buy_date": dip.get("buy_date", ""),
        "sell_trigger": dip.get("sell_trigger", ""),
        "window": dwin["name"], "window_note": dwin["note"],
        "reasons": reasons, "dip": dip,
    }


def _risk_text(q, meta, st):
    risks = []
    if meta.get("ma60") and q["cur"] < meta["ma60"]:
        risks.append("价在MA60下方(弱势)，回补后不加仓")
    if st.get("state") in (WAIT_BUYBACK, SELL_DONE) and not st.get("buyback_price"):
        risks.append("T仓在外，务必日内回补，不裸仓过夜")
    if q["high"] > q["open"] * 1.03:
        risks.append("冲高幅度大，谨防追高卖飞")
    return "；".join(risks) if risks else "破位/急拉时暂停T操作"


# ────────────────────── 手动记录（用户实际成交后调用）──────────────────────

@fill_transaction
def mark_sell(code, price, shares, reason="", t_score=0):
    """用户实际高抛卖出T仓后调用 → 记日志 + 状态 → WAIT_BUYBACK"""
    if analysis_only():
        raise ValueError("预案模式不可记录成交")
    if not positive(price) or not positive(shares) or int(float(shares)) != float(shares):
        raise ValueError("成交价格及整数数量必须大于零")
    current = load_t_state().get(code) or _default_state(code, code)
    if current.get("state") in (SELL_DONE, WAIT_BUYBACK):
        raise ValueError("已有未完成回补，禁止覆盖原成交")
    actual = position_manager.aggregate_positions().get(code)
    if not actual or int(shares) > available_quantity(actual):
        raise ValueError("卖出数量超过T+1可卖持仓")
    pos_map = position_manager.load_positions()
    name = code
    for grp in ("etf", "stock"):
        for p in pos_map.get(grp, []):
            if p["code"] == code:
                name = p.get("name", code)
                break
    _log_trade(code, name, "SELL_T", price, None, shares, None, reason or "卖T高抛", t_score)
    st = load_t_state().get(code) or _default_state(code, name)
    st["state"] = WAIT_BUYBACK
    st["sell_price"] = float(price)
    st["sell_shares"] = int(shares)
    st["buyback_shares"] = 0
    st["sell_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st["date"] = date.today().isoformat()
    _all = load_t_state()
    _all[code] = st
    save_t_state(_all)
    return st


@fill_transaction
def mark_buyback(code, price, shares, reason=""):
    """用户实际低吸回补后调用 → 记日志（含收益） + 状态 → COMPLETE"""
    if analysis_only():
        raise ValueError("预案模式不可记录成交")
    if not positive(price) or not positive(shares) or int(float(shares)) != float(shares):
        raise ValueError("成交价格及整数数量必须大于零")
    current = load_t_state().get(code) or _default_state(code, code)
    remaining = int(current.get("sell_shares") or 0) - int(current.get("buyback_shares") or 0)
    if current.get("state") not in (SELL_DONE, WAIT_BUYBACK) or int(shares) > remaining:
        raise ValueError("回补数量超过未完成卖出数量或状态不符")
    pos_map = position_manager.load_positions()
    name = code
    for grp in ("etf", "stock"):
        for p in pos_map.get(grp, []):
            if p["code"] == code:
                name = p.get("name", code)
                break
    st = load_t_state().get(code) or _default_state(code, name)
    sell_price = st.get("sell_price")
    pnl = None
    if sell_price:
        pnl = round((float(sell_price) - float(price)) * int(shares), 2)
    _log_trade(code, name, "BUY_T", sell_price, price, shares, pnl, reason or "回补低吸", st.get("t_score", 0))
    filled = int(st.get("buyback_shares", 0)) + int(shares)
    st["buyback_shares"] = filled
    st["state"] = COMPLETE if filled == int(st["sell_shares"]) else WAIT_BUYBACK
    st["buyback_price"] = float(price)
    st["buyback_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st["date"] = date.today().isoformat()
    _all = load_t_state()
    _all[code] = st
    save_t_state(_all)
    return st, pnl


def _log_trade(code, name, ttype, sell_price, buy_price, shares, pnl, reason, t_score):
    if _FILL_TX is None:
        raise RuntimeError("成交日志必须与T状态在同一事务保存")
    _FILL_TX["events"].append({"日期": date.today().isoformat(), "代码": code, "名称": name,
        "类型": ttype, "卖出价格": sell_price or 0, "买入价格": buy_price or 0,
        "数量": shares, "收益": pnl if pnl is not None else "", "原因": reason, "T_SCORE": t_score})


# ────────────────────── V2.0 低吸T 成交记录（用户实际成交后必须调用）──────────────────────

def _yesterday_high(code):
    """昨日最高价（突破即卖）；数据不足返回 None"""
    k = _kline(code, 5)
    if len(k) >= 2:
        return round(float(k[-2]["high"]), 3)
    return None


@fill_transaction
def mark_dip_buy(code, price, shares, reason=""):
    """用户实际低吸买入后调用 → 状态 HOLD_T_BUY + 记 DIP_BUY 日志 + 冻结份额"""
    if analysis_only():
        raise ValueError("预案模式不可记录成交")
    if not positive(price) or not positive(shares) or int(float(shares)) != float(shares):
        raise ValueError("成交价格及整数数量必须大于零")
    current = load_t_state().get(code) or _default_state(code, code)
    if (current.get("dip") or {}).get("state") in (HOLD_T_BUY, SELL_T):
        raise ValueError("已有未完成低吸仓，禁止覆盖")
    pos_map = position_manager.load_positions()
    name = code
    for grp in ("etf", "stock"):
        for p in pos_map.get(grp, []):
            if p["code"] == code:
                name = p.get("name", code)
                break
    st = load_t_state().get(code) or _default_state(code, name)
    dip = st.setdefault("dip", _default_dip())
    dip["state"] = HOLD_T_BUY
    dip["buy_price"] = round(float(price), 3)
    dip["buy_shares"] = int(shares)
    dip["buy_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dip["buy_date"] = date.today().isoformat()
    dip["stop_loss"] = round(float(price) * (1 - DIP_SL), 3)
    dip["target1"] = round(float(price) * (1 + DIP_TP1), 3)
    dip["target2"] = round(float(price) * (1 + DIP_TP2), 3)
    dip["yesterday_high"] = _yesterday_high(code)
    dip["sell_trigger"] = ""
    dip["last_reason"] = f"低吸买入@{price} x{shares}"
    st["date"] = date.today().isoformat()
    _all = load_t_state()
    _all[code] = st
    save_t_state(_all)
    _log_trade(code, name, "DIP_BUY", None, price, shares, None,
               reason or "低吸T买入", st.get("t_score", 0))
    return st


@fill_transaction
def mark_dip_sell(code, price, shares, reason=""):
    """用户实际低吸卖出后调用 → 算收益 + 记 DIP_SELL 日志 + 状态回 DIP_NORMAL"""
    if analysis_only():
        raise ValueError("预案模式不可记录成交")
    if not positive(price) or not positive(shares) or int(float(shares)) != float(shares):
        raise ValueError("成交价格及整数数量必须大于零")
    current = load_t_state().get(code) or _default_state(code, code)
    current_dip = current.get("dip") or {}
    if current_dip.get("state") not in (HOLD_T_BUY, SELL_T) or int(shares) > int(current_dip.get("buy_shares") or 0):
        raise ValueError("卖出数量超过低吸持仓或状态不符")
    if not current_dip.get("buy_date") or current_dip["buy_date"] >= date.today().isoformat():
        raise ValueError("T+1限制：当日、未来或未知买入日不可卖出")
    pos_map = position_manager.load_positions()
    name = code
    for grp in ("etf", "stock"):
        for p in pos_map.get(grp, []):
            if p["code"] == code:
                name = p.get("name", code)
                break
    st = load_t_state().get(code) or _default_state(code, name)
    dip = st.setdefault("dip", _default_dip())
    buy_price = dip.get("buy_price")
    pnl = round((float(price) - float(buy_price)) * int(shares), 2) if buy_price else None
    _log_trade(code, name, "DIP_SELL", price, buy_price, shares, pnl,
               reason or f"低吸T卖出({dip.get('sell_trigger', '')})", st.get("t_score", 0))
    remaining = int(dip["buy_shares"]) - int(shares)
    dip["buy_shares"] = remaining
    if dip.get("sell_trigger") == "目标1半仓":
        left_target = max(0, int(dip.get("target_sell_shares", shares)) - int(shares))
        dip["target_sell_shares"] = left_target
        dip["target1_done"] = left_target == 0
    dip["state"] = HOLD_T_BUY if not dip.get("target_sell_shares") else SELL_T
    if remaining == 0:
        st["dip"] = _default_dip()
    st["date"] = date.today().isoformat()
    _all = load_t_state()
    _all[code] = st
    save_t_state(_all)
    return st, pnl


# ────────────────────── 复盘统计（第十三部分）──────────────────────

def review_stats():
    rows = []
    if os.path.isfile(T_LOG):
        with open(T_LOG, encoding="utf-8") as f:
            rows.extend(csv.DictReader(f))
    rows.extend(load_t_state().get("__fills__", []))
    if not rows:
        return "暂无T交易记录"
    lines = ["\n📊 【ETF T策略复盘】已记录成交毛收益，未扣费用；与真实持仓需核对"]
    by_code = {}
    for r in rows:
        by_code.setdefault(r["代码"], []).append(r)
    for code, rs in by_code.items():
        name = rs[0]["名称"]
        sells = [r for r in rs if r["类型"] == "SELL_T"]
        buys = [r for r in rs if r["类型"] in ("BUY_T", "DIP_SELL")]
        done = len(buys)
        pnls = [float(r["收益"]) for r in buys if r.get("收益") not in (None, "")]
        ok = [p for p in pnls if p and p > 0]
        rate = f"{len(ok)/done*100:.0f}%" if done else "—"
        avg = f"{sum(pnls)/len(pnls):.2f}元" if pnls else "—"
        lines.append(f"  {name}({code}): 卖T记录{len(sells)} 回补/低吸卖出成交{done}笔（含部分成交，非完整轮次） 盈利笔占比{rate} 平均毛收益{avg}")
    return "\n".join(lines)


# ────────────────────── CLI ──────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="ETF日内做T引擎 V2.0（正T+低吸T）")
    ap.add_argument("codes", nargs="*", help="ETF代码（默认=持仓中 enable 的ETF）")
    ap.add_argument("--review", action="store_true", help="输出T复盘统计")
    ap.add_argument("--mark-sell", metavar="CODE:PRICE:SHARES", help="记录实际卖T")
    ap.add_argument("--mark-buy", metavar="CODE:PRICE:SHARES", help="记录实际回补")
    ap.add_argument("--dip-buy", metavar="CODE:PRICE:SHARES", help="记录实际低吸买入")
    ap.add_argument("--dip-sell", metavar="CODE:PRICE:SHARES", help="记录实际低吸卖出")
    ap.add_argument("--window", metavar="HH:MM", help="模拟指定时间的交易窗口(测试用)")
    args = ap.parse_args()

    if args.review:
        print(review_stats())
        sys.exit(0)
    if args.mark_sell:
        c, p, s = args.mark_sell.split(":")
        st = mark_sell(c, float(p), int(s))
        print(f"已记录卖T: {c} @{p} x{s} → 状态 {st['state']}")
        sys.exit(0)
    if args.mark_buy:
        c, p, s = args.mark_buy.split(":")
        st, pnl = mark_buyback(c, float(p), int(s))
        print(f"已记录回补: {c} @{p} x{s} → 状态 {st['state']} 收益 {pnl}元")
        sys.exit(0)
    if args.dip_buy:
        c, p, s = args.dip_buy.split(":")
        st = mark_dip_buy(c, float(p), int(s))
        print(f"已记录低吸买入: {c} @{p} x{s} → 低吸状态 {st['dip']['state']}")
        sys.exit(0)
    if args.dip_sell:
        c, p, s = args.dip_sell.split(":")
        st, pnl = mark_dip_sell(c, float(p), int(s))
        print(f"已记录低吸卖出: {c} @{p} x{s} → 收益 {pnl}元，低吸状态 {st['dip']['state']}")
        sys.exit(0)
    if args.window:
        # 测试窗口划分
        print(json.dumps(market_window(args.window), ensure_ascii=False))
        sys.exit(0)

    pos_map = position_manager.load_positions()
    codes = args.codes or [p["code"] for grp in ("etf", "stock")
                           for p in pos_map.get(grp, [])
                           if (p.get("t_config") or {}).get("enable", True)]
    for c in codes:
        rep = analyze(c)
        print("=" * 52)
        print(f"【ETF T策略】{rep.get('name', c)}({c})  状态: {rep.get('state')}  窗口: {rep.get('window')} {rep.get('time', '')}")
        if rep.get("error"):
            print(f"  ⚠️ {rep['error']}")
            continue
        print(f"  今日最高: {rep['today_high_pct']:+.2f}%  当前: {rep['cur_pct']:+.2f}%  日内位置: {rep.get('day_pos')}%  T评分: {rep['t_score']}")
        print(f"  操作: {rep['action']}")
        if rep.get("sell_zone"):
            print(f"  卖出区: {rep['sell_zone']}（T仓{rep.get('shares', 0)}份）")
        if rep.get("buyback_zone"):
            print(f"  回补区: {rep['buyback_zone']}")
        if rep.get("risk"):
            print(f"  风险: {rep['risk']}")
        if rep.get("ref_sell_zone") and not rep.get("sell_zone"):
            print(f"  📍 参考卖T区: {rep['ref_sell_zone']}（预案，评分未触发）")
        if rep.get("ref_buyback_zone") and not rep.get("buyback_zone"):
            print(f"  📍 参考回补区: {rep['ref_buyback_zone']}（预案，评分未触发）")
        for r in rep.get("reasons", []):
            print(f"  - {r}")
        if rep.get("pending_warn"):
            print(f"  ⚠️ {rep['pending_warn']}")
        d = rep.get("dip")
        if d:
            print(f"  ── 低吸T: {d['state']}  评分: {d['score']}  窗口: {d['window']}")
            print(f"  低吸操作: {d['action']}")
            if d.get("buy_zone"):
                print(f"  低吸买入区: {d['buy_zone']}（{d.get('shares', 0)}份）")
            if d.get("sell_zone"):
                print(f"  低吸卖出区: {d['sell_zone']}")
            if d.get("stop_zone"):
                print(f"  低吸止损: {d['stop_zone']}")
            if d.get("ref_buy_zone") and not d.get("buy_zone"):
                print(f"  📍 参考低吸区: {d['ref_buy_zone']}（预案，评分未触发）")
            for r in d.get("reasons", []):
                print(f"    - {r}")
