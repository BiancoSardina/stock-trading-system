#!/usr/bin/env python3
"""
短线套利分析系统 — 统一分析ETF+个股
供 09:30一条龙(09:50) / 11:10 / 12:30一条龙(12:50) / 14:20一条龙(14:40) 四个时段使用
"""
import json, urllib.request, sys, os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import position_manager
import decision_manager

# ETF配置
ETFS = [
    {"code": "510300", "name": "沪深300ETF", "hold": 0},
    {"code": "588000", "name": "科创50ETF",  "hold": 0},
    {"code": "159732", "name": "消费电子ETF", "hold": 0},
    {"code": "518880", "name": "黄金ETF",    "hold": 0},
]
EXTRA_ETFS = [
    {"code": "159516", "name": "半导体设备ETF"},
    {"code": "159858", "name": "创新药指ETF"},
    {"code": "512170", "name": "医疗ETF"},
    {"code": "588750", "name": "科创芯片ETF汇添富"},
]
# 观察清单（hold=0，只出信号不参与组合计算，有买入机会时提示）
WATCH_ETFS = [
    {"code": "515880", "name": "通信ETF",    "hold": 0},
    {"code": "562800", "name": "稀有金属ETF", "hold": 0},
    {"code": "512400", "name": "有色金属ETF", "hold": 0},
    {"code": "515220", "name": "煤炭ETF",    "hold": 0},
    {"code": "512170", "name": "医疗ETF",    "hold": 0},
    {"code": "513180", "name": "恒生科技ETF", "hold": 0},
    {"code": "510880", "name": "红利ETF",    "hold": 0},
    {"code": "512100", "name": "中证1000ETF", "hold": 0},
    {"code": "159869", "name": "游戏ETF",    "hold": 0},
]

# 个股配置
try:
    from stock_config import STOCKS
except:
    STOCKS = []

# 次日监测名单（18:00选股任务写入，盘中重点跟踪；未买入次日自动移出）
WATCH_STOCKS = []  # [{code, name, added, reason}]
def load_watch_stocks():
    """读取 watchlist.json 的监测个股，合并进 STOCKS 分析列表"""
    global WATCH_STOCKS
    WATCH_STOCKS = []
    try:
        wl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")
        with open(wl_path, encoding="utf-8") as _f:
            _data = json.load(_f)
        WATCH_STOCKS = _data.get("stocks", []) or []
    except Exception:
        WATCH_STOCKS = []
    return WATCH_STOCKS

# 股票池（17:30 stock_pool.py 五因子选股；18:00 stock_pool_ai.py AI裁决 → watchlist）
STOCK_POOL = {}  # {date, market_status, market_score, core, watch, valid, stale_days}
def load_stock_pool():
    """读取 stock_pool.json，date 新鲜度校验（V1.1 三路合并第1路）。

    规则：core 全量逐只分析 + watch 简略一行。
    date 非最近交易日（间隔>4自然日/未来日期/解析失败）→ valid=False，
    个股覆盖回退为 stock_config + watchlist，并提示 17:30 重新生成。
    """
    global STOCK_POOL
    STOCK_POOL = {"date": "", "market_status": "", "market_score": "",
                  "core": [], "watch": [], "valid": False, "stale_days": 0}
    try:
        _sp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_pool.json")
        with open(_sp, encoding="utf-8") as _f:
            _data = json.load(_f)
        _date = str(_data.get("date", ""))[:10]
        today = datetime.now().strftime("%Y-%m-%d")
        stale = 0
        try:
            _d = datetime.strptime(_date, "%Y-%m-%d").date()
            _t = datetime.strptime(today, "%Y-%m-%d").date()
            stale = (_t - _d).days
        except Exception:
            stale = -1  # 解析失败
        # 新鲜：date 不晚于今天 且 间隔 0~4 自然日（覆盖周末/短假）
        valid = bool(_date) and 0 <= stale <= 4
        STOCK_POOL = {
            "date": _date,
            "market_status": _data.get("market_status", ""),
            "market_score": _data.get("market_score", ""),
            "core": _data.get("core_pool", []) or [],
            "watch": _data.get("watch_pool", []) or [],
            "valid": valid,
            "stale_days": stale,
        }
    except Exception:
        pass
    return STOCK_POOL

# 资金池（2026-08-05 用户确认：现有可用资金池只有 2万，非原10万假设）
# 买入建议金额 = total_amount × 等级比例（S/A 2%~5% = 400~1000元，B 2% = 400元）
TOTAL_ETF = 20000
TOTAL_STOCK = 20000
WATCH_DETAIL_TOP = 5  # V1.7 筛选方案A（2026-08-26）：股票池 watch 只逐只分析综合分 top5，其余一行简略

# 策略版本号（v2.31 补丁：每次策略修改递增，写入 signal_log 供按版本复盘）
STRATEGY_VERSION = "v2.31"

# ===== 持仓映射（权威数据源：position_manager 的 ~/.hermes/scripts/positions.json）=====
POS_MAP = {}
def load_positions_map():
    """加载权威持仓 {code: {buy_price, buy_date, amount, stop_loss, ...}}，覆盖旧 /home/ubuntu/positions.json"""
    global POS_MAP
    try:
        _data = position_manager.load_positions()
        POS_MAP = {p["code"]: p for grp in ("etf", "stock") for p in _data.get(grp, [])}
    except Exception:
        POS_MAP = {}
    return POS_MAP

# 操作清单汇总（供末尾输出）
ACTION_LIST = []
# V1.6 尾盘确定性结论（2026-08-21 用户要求：尾盘14:45确定性买卖——建仓/加仓/减仓/清仓/持有/不买六选一）
FINAL_LIST = []
# 信号记录（自动写入signal_log.csv，用于复盘胜率）
SIGNAL_LOG = []
# 当前时段（main()设置：早盘/收割后/午后/尾盘），尾盘时买入建议改为"现价直接买"
CURRENT_PERIOD = "午后"
# V1.5 输出过滤（2026-08-21 用户要求）："A"=非持仓B级及以下不输出（定时任务开启）
FILTER_MIN_GRADE = ""

def sina_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0","Referer": "https://finance.sina.com.cn"})
    return urllib.request.urlopen(req, timeout=10).read()

def get_prefix(code):
    # 5/6/9开头=沪市（含56开头沪市ETF如562800稀有金属ETF），其余=深市
    return "sh" if code.startswith(("5", "6", "9")) else "sz"

# 行业推断（第八阶段复盘用）：按名称关键词匹配ETF行业，个股从stock_config映射
_INDUSTRY_MAP = [
    ("半导体", "半导体"), ("芯片", "半导体"), ("通信", "通信"), ("5G", "通信"),
    ("创新药", "医药"), ("医疗", "医药"), ("医药", "医药"), ("生物", "医药"),
    ("有色金属", "有色"), ("有色", "有色"), ("稀土", "有色"), ("煤炭", "煤炭"),
    ("黄金", "黄金"), ("游戏", "传媒"), ("传媒", "传媒"), ("消费电子", "消费电子"),
    ("科创50", "宽基"), ("科创", "科技"), ("沪深300", "宽基"), ("中证1000", "宽基"),
    ("红利", "红利"), ("恒生科技", "港股科技"), ("恒生", "港股"),
]
# 个股行业：代码前缀映射（简化，沪深主板主要行业）
_STOCK_INDUSTRY = {
    "600111": "稀土永磁", "002970": "电子", "600276": "医药", "601179": "电力设备",
    "600089": "电力设备", "688008": "半导体", "601869": "通信", "603629": "电子",
    "002156": "半导体", "603296": "消费电子", "002842": "有色", "600118": "航天军工",
}
def _industry_of(name, code=""):
    """推断标的行业（ETF按名称关键词，个股查映射表）"""
    if code and code in _STOCK_INDUSTRY:
        return _STOCK_INDUSTRY[code]
    for kw, ind in _INDUSTRY_MAP:
        if kw in name:
            return ind
    return "其他"

def get_kline(code, days=120):
    """个股日K线（quotes.sina.cn 抗限流端点；money.finance 连续请求会触发 HTTP 456，标的多时必换）"""
    pref = get_prefix(code)
    url = (f"https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
           f"?symbol={pref}{code}&scale=240&ma=5&datalen={days}")
    try:
        return json.loads(sina_get(url).decode("utf-8", "ignore"))
    except Exception:
        return []

def get_rt(code):
    pref = get_prefix(code)
    data = sina_get(f"https://hq.sinajs.cn/list={pref}{code}").decode("gbk")
    parts = data.split(",")
    if len(parts) >= 10:
        return {"open": float(parts[1]), "prev": float(parts[2]), "cur": float(parts[3]),
                "high": float(parts[4]), "low": float(parts[5]), "vol": int(parts[8])}
    return None

# ===== A股手续费硬性规则（2026-08-06 用户要求：所有分析计划必须体现）=====
# 佣金万2.5最低5元/笔(买卖双向)；卖出个股印花税0.05%；过户费万0.1
# 划算线：单笔≥3000元(5元佣金占比≤0.17%)，低于此档位提示不划算
COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_RATE = 0.0005
TRANSFER_RATE = 0.00001
FEE_OK_AMOUNT = 3000.0

def fee_cost(amount, is_sell=False):
    """单笔手续费(元)：佣金(最低5元)+过户费，卖出个股另加印花税"""
    if not amount or amount <= 0:
        return 0.0
    comm = max(amount * COMMISSION_RATE, COMMISSION_MIN)
    stamp = amount * STAMP_RATE if is_sell else 0
    transfer = amount * TRANSFER_RATE
    return round(comm + stamp + transfer, 2)

def fee_suffix(yuan, is_sell=False):
    """手续费提示后缀：单笔<3000元返回不划算提醒，否则返回空串"""
    if not yuan or yuan <= 0:
        return ""
    cost = fee_cost(yuan, is_sell)
    pct = cost / yuan * 100
    if yuan < FEE_OK_AMOUNT:
        return f" ⚠️手续费{cost}元({pct:.2f}%)，单笔<3000元不划算，量力加大份额"
    return ""

# ===== A股交易规则：买卖按100股/100份(1手)整数倍 =====
def round_lot(yuan, price, lot=100):
    """金额→整手数（100整数倍），最少1手"""
    if not price or price <= 0:
        return lot
    return max(lot, int(yuan / price / lot) * lot)

def lot_text(yuan, price, unit="股", is_sell=False):
    """金额建议→手数建议文本，如 '100股(约1391元)'；单笔<3000元附手续费提醒"""
    if not price or price <= 0:
        return f"~{int(yuan)}元"
    shares = round_lot(yuan, price)
    amt = shares * price
    return f"{shares}{unit}(约{int(amt)}元){fee_suffix(amt, is_sell)}"

def buy_range_text(amt_min, amt_max, price, unit="股"):
    """区间金额→手数区间文本，如 '100~200股(约1391~2782元)'；区间不足3000元附手续费提醒"""
    if not price or price <= 0:
        return f"{int(amt_min)}~{int(amt_max)}元"
    s_min, s_max = round_lot(amt_min, price), max(round_lot(amt_min, price), round_lot(amt_max, price))
    if s_min == s_max:
        return f"{s_min}{unit}(约{int(s_min*price)}元){fee_suffix(s_min*price)}"
    return f"{s_min}~{s_max}{unit}(约{int(s_min*price)}~{int(s_max*price)}元){fee_suffix(s_max*price)}"

def sell_text(amount, price, hold, unit="股"):
    """减仓金额→手数文本；不足1手可减则提示清仓"""
    if not price or price <= 0:
        return f"~{int(amount)}元"
    held_shares = int(hold / price)
    shares = max(100, int(amount / price / 100) * 100)
    if held_shares > 0 and shares >= held_shares:
        return f"清仓(约{int(held_shares*price)}元)"
    # 若已覆盖持仓98%以上（剩余不足一手），按清仓处理
    if held_shares > 0 and shares * price >= hold * 0.98:
        return f"清仓(约{int(held_shares*price)}元)"
    return f"减仓{shares}{unit}(约{int(shares*price)}元){fee_suffix(shares*price, is_sell=True)}"

# ==================== 四重退出系统（第五阶段核心）====================
# 唯一算法实现在 position_manager.build_exit_plan（本文件委托调用）：
# 1) 止损退出：跌破 MA20 或 买入价-2×ATR（与固定止损取高者=先触发那条）→ 无条件清仓
# 2) 趋势退出：盈利持仓上涨中不提前卖；收盘跌破MA10→减仓50%；跌破MA20→清仓
# 3) 盈利保护：盈利≥10%启动，自买入后最高价回撤8% → 卖出
# 4) 时间退出：买入后10个交易日未上涨（资金效率低）→ 卖出
def build_exit_plan(code, name, cur, kline, ma10, ma20, atr14, pos, no_sell=False, unit="份"):
    """
    计算四重退出信号（委托 position_manager.build_exit_plan，参数保留兼容）
    pos: {buy_price, buy_date, amount, stop_loss, ...}
    返回 (lines, triggered)：lines为输出行；triggered为触发的退出信号列表（供汇总表）
    """
    _pos = dict(pos)
    _pos.setdefault("code", code)
    _pos.setdefault("name", name)
    return position_manager.build_exit_plan(_pos, cur, kline, no_sell=no_sell)

def _position_health_report():
    """持仓健康评分（V3.0 第10节，Phase 3）：趋势30+行业20+资金20+盈利状态20+风险10
    结论：≥80 继续持有 / 60-80 观察 / <60 降低仓位（套牢票=做T减磅，禁清仓）。
    尾盘风控报告调用（main() 内，POS_MAP/EM_EXTRA/STOCK_POOL 已就绪）。"""
    if not POS_MAP:
        return ""
    no_sell_codes = position_manager.trapped_codes()
    lines = ["\n🧭 【持仓健康评分】趋势30+行业20+资金20+盈利20+风险10 | ≥80持有 / 60-80观察 / <60降仓"]
    for code in POS_MAP:
        pos = POS_MAP[code]
        name = pos.get("name", code)
        try:
            rt = get_rt(code)
            if not rt:
                continue
            cur = rt["cur"]
            kline = get_kline(code, 70)
            if not kline or len(kline) < 30:
                continue
            closes = [float(k["close"]) for k in kline]
            highs = [float(k["high"]) for k in kline]
            lows = [float(k["low"]) for k in kline]
            ma20 = calc_ma(closes, 20)
            ma60 = calc_ma(closes, 60)
            ma60_slope = None
            if len(closes) >= 65:
                ma60_prev = sum(closes[-65:-5]) / 60
                ma60_slope = round((ma60 - ma60_prev) / ma60_prev * 100, 2) if ma60_prev else None
            atr14 = calc_atr(highs, lows, closes, 14)
            atr_pct = round(atr14 / cur * 100, 2) if atr14 and cur else None

            # ① 趋势 30（价vsMA20 12 + MA20vsMA60 10 + MA60斜率 8）
            s_trend = 0
            if ma20 and cur > ma20:
                s_trend += 12
            elif ma20 and cur > ma20 * 0.97:
                s_trend += 6
            if ma20 and ma60:
                if ma20 > ma60:
                    s_trend += 10
                elif ma20 >= ma60 * 0.99:
                    s_trend += 5
            if ma60_slope is not None:
                if ma60_slope > 0.3:
                    s_trend += 8
                elif ma60_slope > 0:
                    s_trend += 6
                elif ma60_slope > -0.5:
                    s_trend += 3
            # ② 行业 20（股票池行业评分；池外中性12）
            s_indu = 12
            for _e in (STOCK_POOL.get("core", []) + STOCK_POOL.get("watch", [])):
                if _e.get("code") == code:
                    try:
                        s_indu = min(int(float(_e.get("industry_score", 0))), 20)
                    except Exception:
                        pass
                    break
            # ③ 资金 20（东财主力净流入占比 f184）
            s_fund = 10
            _em = EM_EXTRA.get(code)
            if _em and _em.get("inflow_pct") is not None:
                try:
                    _v = float(_em["inflow_pct"])
                    if _v > 5:
                        s_fund = 20
                    elif _v > 2:
                        s_fund = 17
                    elif _v > 0:
                        s_fund = 14
                    elif _v > -2:
                        s_fund = 10
                    elif _v > -5:
                        s_fund = 7
                    else:
                        s_fund = 4
                except Exception:
                    pass
            # ④ 盈利状态 20（盈亏幅度映射）
            cost = float(pos.get("buy_price") or 0)
            pnl_pct = round((cur - cost) / cost * 100, 2) if cost else 0
            if pnl_pct >= 10:
                s_pnl = 20
            elif pnl_pct >= 5:
                s_pnl = 17
            elif pnl_pct >= 0:
                s_pnl = 13
            elif pnl_pct >= -5:
                s_pnl = 9
            elif pnl_pct >= -10:
                s_pnl = 6
            else:
                s_pnl = 3
            # ⑤ 风险 10（ATR% 越低越稳）
            if atr_pct is None:
                s_risk = 5
            elif atr_pct <= 2:
                s_risk = 9
            elif atr_pct <= 3.5:
                s_risk = 7
            elif atr_pct <= 5:
                s_risk = 5
            elif atr_pct <= 8:
                s_risk = 3
            else:
                s_risk = 1
            total = s_trend + s_indu + s_fund + s_pnl + s_risk
            _icon = "🟢" if total >= 80 else ("🟡" if total >= 60 else "🔴")
            _concl = "继续持有" if total >= 80 else ("观察" if total >= 60 else "降低仓位")
            _extra = "（套牢票：降仓=做T减磅，禁清仓）" if total < 60 and code in no_sell_codes else ""
            lines.append(
                f"  {_icon} {name}({code}) 健康分 {total}/100 "
                f"[趋势{s_trend} 行业{s_indu} 资金{s_fund} 盈利{s_pnl} 风险{s_risk}] "
                f"→ {_concl}{_extra} 现价{cur} 盈亏{pnl_pct:+.1f}%"
            )
        except Exception:
            continue
    return "\n".join(lines)


def get_indices():
    data = sina_get("https://hq.sinajs.cn/list=sh000001,sz399006,sh000688").decode("gbk")
    result = {}
    for line in data.split(";\n"):
        if "hq_str_" not in line: continue
        parts = line.split(",")
        if len(parts) < 4: continue
        name = parts[0].split('"')[-1] if '"' in parts[0] else "?"
        result[name] = {"price": float(parts[3]), "prev": float(parts[2]),
                        "chg": round((float(parts[3])-float(parts[2]))/float(parts[2])*100, 2)}
    return result

def calc_ema(prices, n):
    if not prices or len(prices) < n: return None
    mul = 2/(n+1); ema = sum(prices[:n])/n
    for p in prices[n:]: ema = (p-ema)*mul + ema
    return round(ema, 3)

def calc_rsi(prices, n=14):
    if len(prices) < n+1: return None
    g = l = 0
    for i in range(-n, 0):
        d = prices[i] - prices[i-1]
        if d > 0: g += d
        else: l += abs(d)
    ag, al = g/n, l/n
    return round(100 - 100/(1+ag/al), 1) if al != 0 else 100

def calc_ma(prices, n):
    if not prices or len(prices) < n: return None
    return round(sum(prices[-n:])/n, 3)

def calc_atr(highs, lows, closes, n=14):
    """ATR14：平均真实波幅（衡量波动率，用于止损距离）"""
    if len(closes) < n+1: return None
    trs = []
    for i in range(-n, 0):
        h, l, pc = highs[i], lows[i], closes[i-1]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return round(sum(trs)/n, 4)

def get_index_kline(symbol, days=120):
    """指数K线（quotes.sina.cn 抗限流端点；symbol 形如 sh000001/sz399006），用于RS相对强度对比"""
    url = (f"https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
           f"?symbol={symbol}&scale=240&ma=5&datalen={days}")
    try:
        return json.loads(sina_get(url).decode("utf-8", "ignore"))
    except Exception:
        return []

# ===== 市场环境评分 Market Score（第一维度：仓位/风险总闸门） =====
# 四维加权：指数趋势30 + 赚钱效应30 + 成交量20 + 外部环境20 = 0~100分
# 状态分级：A强势(≥80,仓位80%) / B正常(65~79,仓位60%) / C防守(50~64,仓位30%) / D禁止交易(<50,仓位10%)
MARKET = {}  # 全局市场评分结果，供 analyze_item 等后续决策引用

def em_get(url, ref="https://quote.eastmoney.com/", timeout=10):
    """东方财富接口请求"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": ref})
    return urllib.request.urlopen(req, timeout=timeout).read()

def _trend_score(indices):
    """① 指数趋势分(30)：上证/创业板/科创50 现价 vs MA20/MA60，每个指数10分"""
    score, detail = 0, []
    for sym, nm in [("sh000001", "上证指数"), ("sz399006", "创业板指"), ("sh000688", "科创50")]:
        try:
            kl = get_index_kline(sym, 90)
            closes = [float(k["close"]) for k in kl]
            cur = (indices or {}).get(nm, {}).get("price") or closes[-1]
            ma20 = sum(closes[-20:]) / 20
            ma60 = sum(closes[-60:]) / 60
            if cur > ma20:
                score += 10
                detail.append(f"{nm}站上MA20✓")
            elif cur > ma60:
                score += 5
                detail.append(f"{nm}破MA20守MA60△")
            else:
                detail.append(f"{nm}破MA60✗")
        except Exception:
            detail.append(f"{nm}数据缺失")
    return score, detail

def _breadth_score():
    """② 赚钱效应分(30)：涨跌家数15 + 涨停/跌停15"""
    score, detail = 0, []
    # 涨跌家数（东财 ulist：沪市000001 + 深市综指399106 一次请求）
    up = dn = fl = 0
    try:
        url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2"
               "&secids=1.000001,0.399106&fields=f2,f3,f104,f105,f106")
        d = json.loads(em_get(url).decode("utf-8", "ignore"))
        for it in d["data"]["diff"]:
            up += int(it.get("f104", 0) or 0)
            dn += int(it.get("f105", 0) or 0)
            fl += int(it.get("f106", 0) or 0)
        total = up + dn + fl
        ratio = up / total if total else 0
        if ratio >= 0.65: s = 15
        elif ratio >= 0.55: s = 12
        elif ratio >= 0.45: s = 8
        elif ratio >= 0.35: s = 4
        else: s = 0
        score += s
        detail.append(f"涨{up}/跌{dn}/平{fl}(上涨{ratio*100:.0f}%得{s}分)")
    except Exception:
        score += 8
        detail.append("涨跌家数数据缺失(中性8分)")
    # 涨停/跌停家数（东财涨停池/跌停池 data.tc，无数据自动回退前一交易日）
    def pool_count(path):
        for back in range(8):
            day = (datetime.now() - timedelta(days=back)).strftime("%Y%m%d")
            try:
                url = (f"https://push2ex.eastmoney.com/{path}?ut=7eea3edcaed734bea9cbfc24409ed989"
                       f"&dpt=wz.ztzt&Pageindex=0&pagesize=1&sort=fbt%3Aasc&date={day}")
                d = json.loads(em_get(url).decode("utf-8", "ignore"))
                tc = (d.get("data") or {}).get("tc")
                if tc is not None:
                    return int(tc)
            except Exception:
                continue
        return None
    zt = pool_count("getTopicZTPool")
    dt = pool_count("getTopicDTPool")
    if zt is None or dt is None:
        score += 8
        detail.append("涨停/跌停数据缺失(中性8分)")
    else:
        net = zt - dt
        if net >= 50: s = 15
        elif net >= 20: s = 12
        elif net >= 5: s = 9
        elif net >= 0: s = 5
        elif net >= -10: s = 2
        else: s = 0
        score += s
        detail.append(f"涨停{zt}/跌停{dt}(净{net:+d}得{s}分)")
    return score, detail

def _volume_score():
    """③ 成交量分(20)：上证指数成交额 放量涨/缩量涨/放量跌（今日折算 vs 前5日均额）"""
    try:
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000001"
               "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f59"
               "&klt=101&fqt=1&end=20500101&lmt=30")
        d = json.loads(em_get(url).decode("utf-8", "ignore"))
        rows = [k.split(",") for k in d["data"]["klines"]]
        amts = [float(r[6]) for r in rows]      # 成交额(元)
        chgs = [float(r[7]) for r in rows]      # 涨跌幅%
        now = datetime.now()
        hm = now.hour * 60 + now.minute
        open_t, close_t = 9 * 60 + 30, 15 * 60
        if hm < 10 * 60:
            # 早盘(10:00前)当日累计量无意义：用昨日完整量比
            vr = amts[-2] / (sum(amts[-6:-2]) / 4) if amts[-2] else 1
            chg, tag = chgs[-2], "昨日"
        else:
            prog = min(1.0, max(0.05, (hm - open_t) / (close_t - open_t)))  # 盘中时间进度折算
            vr = (amts[-1] / prog) / (sum(amts[-6:-1]) / 5) if amts[-1] else 1
            chg, tag = chgs[-1], "今日"
        amt_now = amts[-1] / 1e8
        if chg > 0:
            if vr >= 1.2: s, st = 20, "放量上涨"
            elif vr >= 0.9: s, st = 15, "平量上涨"
            else: s, st = 10, "缩量上涨(动能不足)"
        else:
            if vr >= 1.2: s, st = 0, "放量下跌(恐慌)"
            elif vr >= 0.9: s, st = 6, "平量下跌"
            else: s, st = 10, "缩量下跌(抛压减轻)"
        return s, f"{st}：{tag}量比{vr:.2f}({amt_now:.0f}亿/折算日均)得{s}分"
    except Exception:
        return 10, "量能数据缺失(中性10分)"

def _external_score():
    """④ 外部环境分(20)：隔夜纳指8 + 英伟达6 + 美元6（东财f170=涨跌幅×100）"""
    score, detail = 0, []
    def f170(sec):
        url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={sec}&fields=f170,f58"
        d = json.loads(em_get(url).decode("utf-8", "ignore"))
        return d["data"]["f170"] / 100.0
    try:
        ndx = f170("100.NDX")
        s = 8 if ndx >= 1 else 6 if ndx >= 0 else 2 if ndx >= -1 else 0
        score += s
        detail.append(f"纳指{ndx:+.2f}%({s}分)")
    except Exception:
        score += 4
        detail.append("纳指缺失(中性4分)")
    try:
        nvda = f170("105.NVDA")
        s = 6 if nvda >= 1 else 4 if nvda >= 0 else 1 if nvda >= -1 else 0
        score += s
        detail.append(f"英伟达{nvda:+.2f}%({s}分)")
    except Exception:
        score += 3
        detail.append("英伟达缺失(中性3分)")
    try:
        usd = f170("100.UDI")
        s = 6 if usd <= -0.2 else 3 if usd <= 0.2 else 1
        score += s
        detail.append(f"美元{usd:+.2f}%({s}分)")
    except Exception:
        score += 3
        detail.append("美元缺失(中性3分)")
    return score, detail

def market_score(indices=None):
    """市场环境评分主函数：返回 dict{score,state,position,parts,lines} 并存入全局 MARKET"""
    global MARKET
    if indices is None:
        indices = get_indices()
    t, t_d = _trend_score(indices)
    b, b_d = _breadth_score()
    v, v_d = _volume_score()
    e, e_d = _external_score()
    total = t + b + v + e
    if total >= 80: state, pos, icon = "A", 80, "🟢"
    elif total >= 65: state, pos, icon = "B", 60, "🟡"
    elif total >= 50: state, pos, icon = "C", 30, "🟠"
    else: state, pos, icon = "D", 10, "🔴"
    state_desc = {
        "A": "市场强势，可积极建仓/加仓，单日仓位上限80%",
        "B": "市场正常，仓位上限60%，追高谨慎",
        "C": "市场防守，仓位上限30%，只低吸不追涨",
        "D": "市场风险高，禁止开新仓！只处理止损/减仓",
    }[state]
    lines = [f"\n🏛️ 【市场环境评分】Market Score",
             f"  市场状态: {icon} {state}  |  市场评分: {total}分  |  允许仓位: {pos}%",
             f"  ① 指数趋势 {t}/30 | {' | '.join(t_d)}",
             f"  ② 赚钱效应 {b}/30 | {' | '.join(b_d)}",
             f"  ③ 成交量   {v}/20 | {v_d}",
             f"  ④ 外部环境 {e}/20 | {' | '.join(e_d)}",
             f"  💡 {state_desc}"]
    MARKET = {"score": total, "state": state, "position": pos, "parts": {"trend": t, "breadth": b, "volume": v, "external": e}}
    return lines

# ===== 五因子评分模型（0~100） =====
# 趋势30 + 动量20 + 资金20 + 相对强度20 + 风险10
# 等级：S≥80 / A 65~79 / B 50~64 / C 35~49 / D<35
EM_EXTRA = {}  # code -> {"turnover": 换手率%, "inflow_pct": 主力净流入占比%}（东财，失败时为空走中性分）

def _secid(code):
    return ("1." if code.startswith(("5", "6", "9")) else "0.") + code

def fetch_em_extra(codes):
    """东财 ulist 单次批量抓取 换手率(f8)+主力净流入占比(f184)，供资金因子使用"""
    if not codes:
        return
    secids = ",".join(_secid(c) for c in codes)
    try:
        url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2"
               f"&secids={secids}&fields=f12,f8,f184")
        d = json.loads(em_get(url, timeout=6).decode("utf-8", "ignore"))
        for it in (d.get("data") or {}).get("diff") or []:
            code = str(it.get("f12") or "")
            if code:
                EM_EXTRA[code] = {"turnover": it.get("f8"), "inflow_pct": it.get("f184")}
    except Exception:
        pass  # 失败则各标的走中性分兜底

def score_grade(score):
    """0~100 → (等级, 描述)
    V1.4（2026-08-12 规则体检）：A级门槛 65→70——实测 60-69 分段5日胜率仅31.4%
    且均涨-0.78%（负期望，见 rule_audit6），原A级(65-79)含该负段；70-79 胜率69%才合格。
    B级(50-69)胜率<50%：取消"轻仓试探"，下游只给观察/观望（quality=="B"分支已改）。
    """
    if score >= 80: return "S", "五因子高分，多指标共振看多！高置信度"
    if score >= 70: return "A", "信号偏多，可操作"
    if score >= 50: return "B", "中性偏多，观察为主（胜率不足，不轻仓试探）"
    if score >= 35: return "C", "中性偏弱，观望为主"
    return "D", "偏空，减仓/禁买"

def _f_trend(cur, ma10, ma20, ma60, ma60_slope, bm, wma5, wma10):
    """① 趋势因子(30)：价vsMA20 5 + 布林中轨 1 + MA20>MA60 8 + MA60斜率 8 + 周线 8"""
    s, d = 0, []
    if ma20 and cur > ma20 * 1.03: s += 5; d.append("价>MA20+3%")
    elif ma20 and cur > ma20: s += 4; d.append("价>MA20")
    elif ma10 and cur > ma10: s += 2; d.append("价>MA10")
    elif ma60 and cur > ma60: s += 1; d.append("价>MA60")
    else: d.append("价破MA60")
    if bm and cur > bm: s += 1; d.append("布林中轨上")
    if ma20 and ma60:
        if ma20 > ma60: s += 8; d.append("MA20>MA60")
        elif ma20 >= ma60 * 0.99: s += 4; d.append("MA20≈MA60粘合")
        else: d.append("MA20<MA60")
    if ma60_slope is not None:
        if ma60_slope > 1: s += 8; d.append(f"MA60↑{ma60_slope:+.1f}%")
        elif ma60_slope > 0.3: s += 7; d.append(f"MA60↑{ma60_slope:+.1f}%")
        elif ma60_slope > 0: s += 6; d.append(f"MA60微↑{ma60_slope:+.1f}%")
        elif ma60_slope > -0.5: s += 3; d.append(f"MA60微↓{ma60_slope:+.1f}%")
        else: d.append(f"MA60↓{ma60_slope:+.1f}%")
    if wma5 and wma10:
        if cur > wma5 > wma10: s += 8; d.append("周线多头")
        elif cur > wma10: s += 5; d.append("周线偏多")
        elif cur > wma5: s += 3; d.append("周线纠缠")
        else: d.append("周线空头")
    return min(s, 30), " ".join(d)

def _f_momentum(rsi14, dif, dea, chg20, chg20_cap=6):
    """② 动量因子(20)：RSI 7 + MACD 7 + 20日涨幅 6
    chg20_cap=3 时走收紧档（股票池V1.3用：涨得高不再自动拿高分）；默认6=原行为，盘中分析不变。"""
    s, d = 0, []
    if rsi14 is not None:
        if 55 <= rsi14 <= 70: s += 7; d.append(f"RSI{rsi14}健康多头")
        elif 45 <= rsi14 < 55: s += 6; d.append(f"RSI{rsi14}中性偏强")
        elif 70 < rsi14 <= 80: s += 5; d.append(f"RSI{rsi14}偏高(防超买)")
        elif 40 <= rsi14 < 45: s += 4; d.append(f"RSI{rsi14}中性")
        elif rsi14 > 80: s += 3; d.append(f"RSI{rsi14}超买(警惕回调)")
        elif 30 <= rsi14 < 40: s += 2; d.append(f"RSI{rsi14}偏弱")
        else: s += 1; d.append(f"RSI{rsi14}超卖(无动量)")
    if dif is not None and dea is not None:
        if dif > 0 and dif > dea: s += 7; d.append("MACD零轴上金叉")
        elif dif > dea: s += 5; d.append("MACD金叉")
        elif dif > 0: s += 2; d.append("MACD零轴上死叉")
        else: d.append("MACD零轴下死叉")
    if chg20 is not None:
        if chg20_cap <= 3:
            # 收紧档（V1.3）：涨幅加分减半，>15% 封顶3分（原6分）
            if chg20 > 15: s += 3; d.append(f"20日+{chg20:.1f}%")
            elif chg20 > 5: s += 2; d.append(f"20日+{chg20:.1f}%")
            elif chg20 > 0: s += 1; d.append(f"20日+{chg20:.1f}%")
            elif chg20 > -5: s += 1; d.append(f"20日{chg20:.1f}%")
            else: d.append(f"20日{chg20:.1f}%(弱)")
        else:
            if chg20 > 10: s += 6; d.append(f"20日+{chg20:.1f}%")
            elif chg20 > 5: s += 5; d.append(f"20日+{chg20:.1f}%")
            elif chg20 > 0: s += 4; d.append(f"20日+{chg20:.1f}%")
            elif chg20 > -5: s += 2; d.append(f"20日{chg20:.1f}%")
            else: d.append(f"20日{chg20:.1f}%(弱)")
    return min(s, 20), " ".join(d)

def _f_fund(vr, chg, em, is_etf):
    """③ 资金因子(20)：量能+方向 8 + 换手率 6 + 主力资金 6"""
    s, d = 0, []
    vs = 0
    if vr is not None:
        if vr > 1.5: vs = 5
        elif vr >= 1.2: vs = 4
        elif vr >= 0.8: vs = 3
        else: vs = 1
    if vr is not None and vr > 1.2 and chg > 0: vs = min(8, vs + 3); d.append(f"量比{vr}放量涨")
    elif vr is not None and vr > 1.2 and chg < 0: vs = max(0, vs - 2); d.append(f"量比{vr}放量跌")
    elif vr is not None and vr >= 1.0 and chg > 0: vs = min(8, vs + 1); d.append(f"量比{vr}温和放量涨")
    else: d.append(f"量比{vr}")
    s += vs
    to = (em or {}).get("turnover")
    if to is not None:
        if is_etf:
            if to >= 2: s += 6
            elif to >= 1: s += 5
            elif to >= 0.5: s += 4
            elif to >= 0.2: s += 3
            else: s += 2
        else:
            if to >= 5: s += 6
            elif to >= 2: s += 5
            elif to >= 1: s += 4
            elif to >= 0.5: s += 3
            else: s += 2
        d.append(f"换手{to}%")
    else:
        s += 3 if (vr or 1) >= 1.2 else 2
        d.append("换手缺失(中性)")
    inf = (em or {}).get("inflow_pct")
    if inf is not None:
        if inf >= 15: s += 6
        elif inf >= 8: s += 5
        elif inf >= 3: s += 4
        elif inf >= 0: s += 3
        elif inf >= -5: s += 2
        else: s += 1
        d.append(f"主力净{inf:+.1f}%")
    else:
        s += 3
        d.append("资金流缺失(中性)")
    return min(s, 20), " ".join(d)

def _f_rs(rs20, rs20_300, chg20, chg20_cap=6):
    """④ 相对强度(20)：跑赢上证 8 + 跑赢沪深300 6 + 20日绝对强度 6
    chg20_cap=3 时走收紧档（股票池V1.3用）；默认6=原行为，盘中分析不变。"""
    s, d = 0, []
    if rs20 is not None:
        if rs20 > 10: s += 8; d.append(f"跑赢上证{rs20:+.1f}%")
        elif rs20 > 5: s += 7; d.append(f"跑赢上证{rs20:+.1f}%")
        elif rs20 > 0: s += 6; d.append(f"跑赢上证{rs20:+.1f}%")
        elif rs20 > -5: s += 3; d.append(f"跑输上证{rs20:+.1f}%")
        else: d.append(f"大幅跑输上证{rs20:+.1f}%")
    else:
        s += 4; d.append("上证RS缺失(中性)")
    if rs20_300 is not None:
        if rs20_300 > 5: s += 6; d.append(f"跑赢沪深300 {rs20_300:+.1f}%")
        elif rs20_300 > 0: s += 5; d.append(f"跑赢沪深300 {rs20_300:+.1f}%")
        elif rs20_300 > -5: s += 2; d.append(f"跑输沪深300 {rs20_300:+.1f}%")
        else: d.append(f"跑输沪深300 {rs20_300:+.1f}%")
    else:
        s += 3; d.append("沪深300RS缺失(中性)")
    if chg20 is not None:
        if chg20_cap <= 3:
            # 收紧档（V1.3）：绝对强度加分减半，>20% 封顶3分（原6分）
            if chg20 > 20: s += 3; d.append(f"20日+{chg20:.1f}%强")
            elif chg20 > 10: s += 2; d.append(f"20日+{chg20:.1f}%")
            elif chg20 > 0: s += 1; d.append(f"20日+{chg20:.1f}%")
            else: d.append(f"20日{chg20:.1f}%弱")
        else:
            if chg20 > 15: s += 6; d.append(f"20日+{chg20:.1f}%强")
            elif chg20 > 8: s += 5; d.append(f"20日+{chg20:.1f}%强")
            elif chg20 > 3: s += 4; d.append(f"20日+{chg20:.1f}%")
            elif chg20 > 0: s += 3; d.append(f"20日+{chg20:.1f}%")
            elif chg20 > -8: s += 1; d.append(f"20日{chg20:.1f}%弱")
            else: d.append(f"20日{chg20:.1f}%很弱")
    return min(s, 20), " ".join(d)

def _f_risk(atr_pct, max_dd20):
    """⑤ 风险因子(10)：ATR波动 5 + 20日最大回撤 5（波动越低分越高）"""
    s, d = 0, []
    if atr_pct is not None:
        if atr_pct < 2: s += 5
        elif atr_pct < 3: s += 4
        elif atr_pct < 4: s += 3
        elif atr_pct < 5: s += 2
        elif atr_pct < 7: s += 1
        else: s += 0
        d.append(f"ATR{atr_pct}%")
    else:
        s += 3; d.append("ATR缺失(中性)")
    if max_dd20 is not None:
        if max_dd20 > -5: s += 5
        elif max_dd20 > -8: s += 4
        elif max_dd20 > -12: s += 3
        elif max_dd20 > -18: s += 2
        else: s += 1
        d.append(f"20日回撤{max_dd20:.1f}%")
    else:
        s += 2; d.append("回撤缺失(中性)")
    return min(s, 10), " ".join(d)

def analyze_item(code, name, hold, total_amount=TOTAL_ETF, is_etf=True, bench_chg20=None, bench300_chg20=None, no_sell=False, pos=None, from_pool=False):
    """分析单个标的，返回结构化文本"""
    rt = get_rt(code)
    if not rt: return f"\n❌ {name} 数据获取失败"
    try:
        kline = get_kline(code, 120)
        closes = [float(k["close"]) for k in kline]
        highs = [float(k["high"]) for k in kline]
        lows = [float(k["low"]) for k in kline]
        vols = [int(k["volume"]) for k in kline]
    except:
        return f"\n❌ {name} K线失败"

    cur, prev = rt["cur"], rt["prev"]
    chg = round((cur-prev)/prev*100, 2)
    ce = "🟢" if chg >= 0 else "🔴"
    is_watch = hold == 0
    label = "👀 观察中" if is_watch else f"持仓{hold}元"
    
    # 指标
    ma5 = round(sum(closes[-5:])/5, 3) if len(closes)>=5 else None
    ma10 = round(sum(closes[-10:])/10, 3) if len(closes)>=10 else None
    ma20 = round(sum(closes[-20:])/20, 3) if len(closes)>=20 else None
    ma60 = calc_ma(closes, 60)
    # MA60斜率：今日MA60 vs 5日前MA60（趋势三要件之一）
    ma60_slope = None
    if len(closes) >= 65:
        ma60_prev = sum(closes[-65:-5])/60
        ma60_slope = round((ma60 - ma60_prev)/ma60_prev*100, 2)
    rsi14 = calc_rsi(closes)
    rsi6 = calc_rsi(closes, 6)
    atr14 = calc_atr(highs, lows, closes, 14)
    atr_pct = round(atr14/cur*100, 2) if atr14 and cur else None
    # RS相对强度：标的近20日涨幅 - 大盘近20日涨幅
    rs20 = None
    chg20 = round((cur/closes[-21]-1)*100, 2) if len(closes) >= 21 else None
    if chg20 is not None and bench_chg20 is not None:
        rs20 = round(chg20 - bench_chg20, 2)
    dif = calc_ema(closes,12) and round(closes[-1]-calc_ema(closes,12),3) if len(closes)>=26 else None
    dea = None
    if dif and len(closes)>=26:
        dlist = [round(c-calc_ema(closes[:i+1],26),3) for i,c in enumerate(closes) if calc_ema(closes[:i+1],26)]
        if dlist: dea = calc_ema(dlist, 9)
    
    # 周线
    wma5 = round(sum(closes[-25:])/25, 3) if len(closes)>=25 else None
    wma10 = round(sum(closes[-50:])/50, 3) if len(closes)>=50 else None
    
    # 布林
    bm = bt = bb = None
    if len(closes)>=20:
        bm = round(sum(closes[-20:])/20, 3)
        std = (sum((x-bm)**2 for x in closes[-20:])/20)**0.5
        bt = round(bm+2*std, 3); bb = round(bm-2*std, 3)
    
    avgv = sum(vols[-5:])/5
    vr = round(vols[-1]/avgv, 2) if avgv>0 else 1
    
    high20 = round(max(closes[-20:]), 3) if len(closes)>=20 else None
    low20 = round(min(closes[-20:]), 3) if len(closes)>=20 else None
    
    # ===== 五因子评分模型（0~100）：趋势30 + 动量20 + 资金20 + 相对强度20 + 风险10 =====
    # 周线信号（大方向，供 can_buy 与多空计数使用）
    weekly_bull = 0
    if wma5 and wma10:
        if cur > wma5 > wma10: weekly_bull = 1
        elif cur < wma5 < wma10: weekly_bull = -1
    # 20日最大回撤（风险因子）
    max_dd20 = None
    if len(closes) >= 20:
        _peak, _dd = closes[-20], 0.0
        for _c in closes[-20:]:
            _peak = max(_peak, _c)
            _dd = min(_dd, (_c/_peak - 1)*100)
        max_dd20 = round(_dd, 2)
    # RS vs 沪深300（相对强度第二基准，跑赢宽基）
    rs20_300 = None
    if chg20 is not None and bench300_chg20 is not None:
        rs20_300 = round(chg20 - bench300_chg20, 2)

    f_trend, t_d = _f_trend(cur, ma10, ma20, ma60, ma60_slope, bm, wma5, wma10)
    f_mom,   m_d = _f_momentum(rsi14, dif, dea, chg20)
    f_fund,  u_d = _f_fund(vr, chg, EM_EXTRA.get(code, {}), is_etf)
    f_rs,    r_d = _f_rs(rs20, rs20_300, chg20)
    f_risk,  k_d = _f_risk(atr_pct, max_dd20)
    score = min(100, f_trend + f_mom + f_fund + f_rs + f_risk)
    quality, quality_desc = score_grade(score)

    # 多空计数（8项偏多信号，替代旧共振计数）
    bull_n = sum([
        ma20 is not None and cur > ma20,
        ma60 is not None and ma20 is not None and ma20 > ma60,
        ma60_slope is not None and ma60_slope > 0,
        wma5 is not None and wma10 is not None and cur > wma5 > wma10,
        dif is not None and dea is not None and dif > dea,
        rsi14 is not None and rsi14 > 50,
        vr is not None and vr > 1.0 and chg > 0,
        rs20 is not None and rs20 > 0,
    ])
    
    lines = [f"\n{ce} 【{name}({code})】{label} 评分:{score}/100 | 等级:{quality}"]
    lines.append(f"  🧮 五因子: 趋势{f_trend}/30 动量{f_mom}/20 资金{f_fund}/20 强度{f_rs}/20 风险{f_risk}/10")
    # 集合竞价信息（早盘时段：开盘价=竞价撮合结果，竞价量=开盘成交量）
    if CURRENT_PERIOD == "早盘":
        open_px = rt.get("open")
        open_chg = round((open_px - prev)/prev*100, 2) if open_px and prev else None
        auc_vol = rt.get("vol", 0)
        auc_icon = "🟢" if open_chg is not None and open_chg >= 0 else "🔴"
        if open_chg is not None:
            # 竞价强弱判断：高开幅度 + 竞价量 vs 5日均量
            auc_ratio = round(auc_vol/avgv*100, 1) if avgv > 0 else None
            auc_note = ""
            if open_chg >= 2: auc_note = "高开强势(防冲高回落)"
            elif open_chg >= 0.5: auc_note = "小幅高开(正常)"
            elif open_chg > -0.5: auc_note = "平开(方向未定)"
            elif open_chg > -2: auc_note = "小幅低开(观察承接)"
            else: auc_note = "低开弱势(勿抢反弹)"
            lines.append(f"  {auc_icon} 竞价: {auc_icon=='🟢' and '高开' or '低开'}{abs(open_chg):.2f}% (开{open_px} vs 昨收{prev}) | {auc_note}")
            if auc_ratio is not None:
                lines.append(f"  ⚖️ 竞价量: {auc_vol}手({auc_ratio}%×5日均量, {'放量' if auc_ratio > 30 else '正常' if auc_ratio > 10 else '缩量'})")
    lines.append(f"  价:{cur}  涨跌:{chg:+.2f}%  RSI14={rsi14}  量比={vr}")
    lines.append(f"  MA5={ma5} MA10={ma10} MA20={ma20}" + (f" MA60={ma60}" if ma60 else ""))
    # 趋势三要件（MA60向上 + 价在MA60上 + MA20在MA60上）
    trend_parts = []
    if ma60 and ma60_slope is not None:
        trend_parts.append(f"MA60{'↑' if ma60_slope > 0 else '↓'}({ma60_slope:+.2f}%)")
        trend_parts.append(f"价{'在' if cur > ma60 else '破'}MA60")
    if ma20 and ma60:
        trend_parts.append(f"MA20{'↑' if ma20 > ma60 else '↓'}MA60")
    if trend_parts:
        trend_ok = ma60 and ma60_slope and ma60_slope > 0 and cur > ma60 and ma20 and ma20 > ma60
        lines.append(f"  📐 趋势三要件: {'✅全满足' if trend_ok else '⚠️未全满足'} | {' '.join(trend_parts)}")
    # RS相对强度（跑赢大盘才算强势）
    if rs20 is not None:
        rs_icon = "🟢" if rs20 > 0 else "🔴"
        lines.append(f"  {rs_icon} RS强度: 20日{'跑赢' if rs20 > 0 else '跑输'}大盘{abs(rs20):.2f}% (标的{chg20:+.2f}% vs 大盘{bench_chg20:+.2f}%)")
    # ATR波动率
    if atr_pct is not None:
        lines.append(f"  📏 ATR波动率: {atr_pct:.2f}%/日 (止损参考≈{round(atr14*2,3) if atr14 else 0})")
    if bm: lines.append(f"  布林:上{bt} 中{bm} 下{bb}")
    if high20 and low20: lines.append(f"  🎯 支撑{low20}  压力{high20}")
    
    # 五因子拆解明细
    lines.append(f"  📈 {t_d}")
    lines.append(f"  ⚡ {m_d}")
    lines.append(f"  💰 {u_d}")
    lines.append(f"  🏆 {r_d}")
    lines.append(f"  🛡️ {k_d}")
    
    # 多空计数提示
    if bull_n >= 6:
        lines.append(f"  ✅ {bull_n}/8项偏多 → 共振看多")
    elif bull_n <= 2:
        lines.append(f"  🔴 {bull_n}/8项偏多 → 偏空承压")
    else:
        lines.append(f"  📊 {bull_n}/8项偏多")
    
    # 操作建议+置信度
    action_type = None  # buy / sell / hold / watch
    buy_zone = sell_zone = stop_zone = None
    amount_advice = ""
    # V3.0：动作文本暂存，状态机（decision_manager）放行后才输出，防"信号被拦但文本已打印"
    _up_line = ""    # 买入/加仓动作文本（dm 裁决 BUY/ADD 才输出）
    _down_line = ""  # 减仓/清仓动作文本（dm 裁决 REDUCE/SELL 才输出）
    _up_stop = ""    # S级加仓的止损行（随 _up_line 输出）
    unit = "份" if is_etf else "股"  # A股规则：买卖按100股/100份(1手)整数倍
    # 买入纪律：趋势向下(现价<MA20或周线偏空)时禁止买入/加仓，只允许减仓
    can_buy = bool(ma20) and cur > ma20 and weekly_bull != -1
    # 市场总闸门（Market Score）：D级禁止交易时，一切买入/加仓建议降级为观望，只保留减仓/止损
    if MARKET.get("state") == "D":
        can_buy = False
    # 仓位缩放系数：允许仓位相对A级(80%)的比例 → A=1.0 B=0.75 C=0.375，买入/加仓金额按此缩放
    pos_factor = max(MARKET.get("position", 80) / 80.0, 0.1)
    mstate = MARKET.get("state", "A")  # 市场状态，用于追涨限制
    
    # 计算操作点位（动态支撑：近5日低点与MA10取较高者，防止"买入区永远够不着"）
    # 注：日线接口不含当日K线，需并入实时高低点，否则追涨/卖出位会低于现价
    rt_hi = rt.get("high") if rt else None
    rt_lo = rt.get("low") if rt else None
    sup5 = round(min(lows[-5:] + ([rt_lo] if rt_lo else [])), 3) if len(lows) >= 5 else low20
    res5 = round(max(highs[-5:] + ([rt_hi] if rt_hi else [])), 3) if len(highs) >= 5 else high20
    chase_zone = round(res5 * 1.005, 3) if res5 else None  # 突破追涨位（第二个可触发买点）
    if sup5 and res5:
        # 动态支撑 = 近5日低点 与 MA10 的较高者（趋势支撑，贴近现价）
        dyn_sup = sup5
        if ma10 and ma10 < cur:
            dyn_sup = max(sup5, round(ma10, 3))
        # 若动态支撑距现价超过7%（起涨点太远=追不上），买入区上移到现价附近（回踩2-3%）
        if cur and dyn_sup < cur * 0.93:
            dyn_sup = round(cur * 0.97, 3)
        buy_zone = (round(dyn_sup, 3), round(min(cur, dyn_sup * 1.03), 3))
        sell_zone = (round(res5 * 0.98, 3), round(res5, 3))
        stop_zone = round(dyn_sup * 0.97, 3)

    # ===== 买点形态闸门（2026-08-14 昆药复盘新增）：评分高≠买点好，情绪高位禁止现价追 =====
    # 触发任一条：S/A级买入/加仓降级为条件单（回踩企稳买/放量站稳再追），不追现价。
    # 背景：昆药集团08-13在"前日放量大涨+4%→当日冲高回落收长上影"时仍被全天喊买入，
    # 次日止损-3.5%。评分度量"票强不强"，闸门度量"现在是不是上车时机"。
    # 关键区分：拦截"追高位"，放行"回踩低吸位"（强势股回踩企稳是主用买点，不能误杀，
    # 如长江证券08-14：昨日+3.2%今日微跌-0.5%回踩到日内低位=低吸，放行）。
    _gate = []
    # 日内位置（0=最低 100=最高）：现价在日内低位=回踩低吸，高位=追涨
    _pos_pct = None
    _rt_lo = rt.get("low") if rt else None
    if rt_hi and _rt_lo and rt_hi > _rt_lo:
        _pos_pct = (cur - _rt_lo) / (rt_hi - _rt_lo) * 100
    _chasing = _pos_pct is None or _pos_pct >= 40  # 数据缺失时保守按追高处理
    if len(closes) >= 2:
        _prev_chg = (closes[-1] - closes[-2]) / closes[-2] * 100
        if vr and vr >= 1.5 and _prev_chg >= 3 and _chasing:
            _gate.append(f"昨日放量大涨{_prev_chg:.1f}%(量比{vr})现价未回踩到位")
        if vr and vr >= 1.5 and chg is not None and chg <= -1.5:
            _gate.append(f"昨日放量今日大跌({chg:+.1f}%)动能衰竭")
    if _chasing and rt_hi and cur and rt_hi > cur:
        _pull = (rt_hi - cur) / rt_hi * 100
        if _pull >= 3:
            _gate.append(f"当日自高点回落{_pull:.1f}%")
    if len(closes) >= 3:
        _c3 = (cur / closes[-3] - 1) * 100
        # 阈值12%（≈2个涨停）：低于此的启动期强势股属于正常范畴，交给①②③按位置判断；
        # 昆药案例3日最高仅+6.7%，靠①②③拦截，不依赖本条
        if _c3 >= 12:
            _gate.append(f"近3日累计+{_c3:.1f}%过热")


    
    if is_watch:
        is_final = CURRENT_PERIOD == "尾盘"  # 尾盘=主操作窗口，直接可操作，不挂单
        if quality in ("S", "A") and can_buy:
            amt_min, amt_max = int(total_amount*0.02*pos_factor), int(total_amount*0.05*pos_factor)
            if _gate:
                # 形态闸门（2026-08-14 昆药复盘）：评分高但买点差 → 不追现价，改挂条件单
                _gate_txt = "；".join(_gate)
                if is_final:
                    _up_line = f"  ▶ ⚠️【{quality}级】{quality_desc}但买点形态不佳({_gate_txt})→ 尾盘不买，明日回踩{buy_zone[0] if buy_zone else '支撑'}企稳再考虑"
                else:
                    _up_line = f"  ▶ 🟡【{quality}级】{quality_desc}但买点形态不佳({_gate_txt})→ 不追现价，挂条件单：回踩{buy_zone[0] if buy_zone else '支撑'}企稳买" + (f" / 放量站稳{chase_zone}再追" if chase_zone else "")
                action_type = "watch"
                amount_advice = ""
            elif is_final:
                _up_line = f"  ▶ 🟢【{quality}级】{quality_desc}！现价{cur}可直接买入{buy_range_text(amt_min, amt_max, cur, unit)}建仓(吃明日溢价)"
                buy_zone = (round(cur*0.995, 3), round(cur*1.005, 3))  # 尾盘买入=现价附近
            else:
                _up_line = f"  ▶ 🟢【{quality}级】{quality_desc}！建议买入{buy_range_text(amt_min, amt_max, cur, unit)}建仓"
            action_type = "buy"
            amount_advice = buy_range_text(amt_min, amt_max, cur, unit)
        elif quality in ("S", "A"):
            if mstate in ("C", "D"):
                lines.append(f"  ▶ ⚠️ 指标偏多但市场{mstate}级(防守)：观察，站稳MA20({ma20})且市场转好再考虑")
            else:
                lines.append(f"  ▶ ⚠️ 指标偏多但趋势未确认(未站稳MA20/周线偏空)：观察，站稳MA20({ma20})或放量突破{chase_zone}再买")
            action_type = "watch"
        elif quality == "B" and can_buy:
            # V1.4（2026-08-12 体检）：B级5日胜率仅32-44%≈抛硬币，取消轻仓试探，改观察
            _obs = f"继续观察：回踩{buy_zone[0]:.3f}企稳买" if buy_zone else "继续观察"
            if chase_zone:
                _obs += f" / 放量突破{chase_zone}再考虑"
            lines.append(f"  ▶ ⚪【B级】信号中性偏多但历史胜率不足（{quality_desc}），暂不轻仓试探：{_obs}")
            action_type = "watch"
        elif quality == "D":
            lines.append(f"  ▶ 🔴【{quality}级】{quality_desc}：观察，企稳信号=缩量止跌+站回MA5({ma5})，破前低{low20}则放弃")
            action_type = "watch"
        else:
            _obs = f"继续观察：回踩{buy_zone[0]:.3f}企稳买" if buy_zone else "继续观察"
            if chase_zone and mstate not in ("C", "D"):
                _obs += f" / 突破{chase_zone}追" + ("(市场B级仅轻仓)" if mstate == "B" else "")
            elif chase_zone:
                _obs += f" / 市场{mstate}级不追涨只低吸"
            if buy_zone:
                _obs += f" / 破{round(buy_zone[0]*0.97,3):.3f}弃"
            lines.append(f"  ▶ ⚪ {_obs}")
            action_type = "watch"
    elif no_sell:
        # 做T点位：优先当日实时高低点（动态），辅以近3日高低点做参考
        # T+1铁律：当日买入当日不可卖。做T必须"先卖后买"（卖老仓→回落接回），
        # 接回的新仓次日才能再卖；禁止先买后卖式假T。
        # 现价位置判定：接近当日高点(>70%)=高抛区，接近当日低点(<30%)=低吸区
        day_hi, day_lo = rt.get("high"), rt.get("low")
        day_range = (day_hi - day_lo) if day_hi and day_lo else 0
        pos_pct = (cur - day_lo)/day_range*100 if day_range > 0 else 50
        t_high = round(max(highs[-3:]), 3) if len(highs) >= 3 else res5
        t_low = round(min(lows[-3:]), 3) if len(lows) >= 3 else sup5
        t_amount = lot_text(int(hold*0.15), cur, unit)  # 每次做T用15%底仓
        lines.append(f"  ▶ 🔄 套牢持有，做T+加仓摊成本（不清仓）")
        if pos_pct >= 70:
            # 现价在日内高位 → 提示高抛，回落再接
            lines.append(f"  🎯 做T(T+1先卖后买)：现价{cur}已在日内高位({pos_pct:.0f}%)，冲高即可高抛{t_amount}，回落{t_low:.3f}附近接回")
        elif pos_pct <= 30:
            # 现价在日内低位 → 提示低吸，反弹再卖
            lines.append(f"  🎯 做T(T+1先卖后买)：现价{cur}在日内低位({pos_pct:.0f}%)，可先低吸{t_amount}，反弹到{t_high:.3f}附近高抛")
        else:
            lines.append(f"  🎯 做T(T+1先卖后买)：高抛{t_high:.3f}附近卖{t_amount}，回落{t_low:.3f}附近接回")
        # 套牢持仓：只做T，禁止纯加仓（V3.0纪律 2026-08-07 用户拍板；做T=先卖后买摊成本）
        if quality in ("S", "A") and can_buy:
            lines.append(f"  ▶ 🟢【{quality}级】{quality_desc}！趋势转强，可加大做T仓位（先卖后买摊成本），禁止纯加仓")
        elif quality == "B" and can_buy:
            lines.append(f"  ▶ 🟡【B级】{quality_desc}，做T为主")
        else:
            lines.append(f"  ▶ ⚪ 信号未转强，暂以做T为主")
        action_type = "hold"
        if buy_zone:
            amount_advice = lot_text(int(hold*0.05), cur, unit)
    elif quality in ("S", "A") and can_buy and _gate:
        # 形态闸门：连涨过热时加仓同样等回踩（2026-08-14 昆药复盘）
        lines.append(f"  ▶ 🟡【{quality}级】{quality_desc}但买点形态不佳({'；'.join(_gate)})→ 暂不加仓，回踩{buy_zone[0] if buy_zone else 'MA10'}企稳再加")
        action_type = "hold"
    elif quality == "S" and can_buy:
        _up_line = f"  ▶ 🟢🟢【S级】{quality_desc}！可加仓{lot_text(int(hold*0.2*pos_factor), cur, unit)}"
        _up_stop = f"  🛑 止损: 破MA20({ma20})或前低({low20})就走"
        action_type = "buy"
        amount_advice = lot_text(int(hold*0.2*pos_factor), cur, unit)
    elif quality == "A" and can_buy:
        _up_line = f"  ▶ 🟢【A级】{quality_desc}，可加仓{lot_text(int(hold*0.15*pos_factor), cur, unit)}"
        action_type = "buy"
        amount_advice = lot_text(int(hold*0.15*pos_factor), cur, unit)
    elif quality == "B" and can_buy:
        # V1.4（2026-08-12 体检）：持仓B级5日胜率不足，取消小仓位试探，只维持持有
        lines.append(f"  ▶ ⚪【B级】{quality_desc}，历史胜率不足（约32-44%）：维持持有不加仓，站稳MA20且转A级再加")
        action_type = "hold"
    elif quality in ("S", "A", "B"):
        # 修复：S/A级但趋势未确认(现价<MA20/周线偏空)时不再误落 D 级减仓分支
        lines.append(f"  ▶ ⚪ 信号尚可但趋势未确认(未站稳MA20/周线偏空)：观望，站稳MA20({ma20})再考虑加仓")
        action_type = "hold"
    elif quality == "C":
        _down_line = f"  ▶ 🟠【C级】{quality_desc} → {sell_text(int(hold*0.15), cur, hold, unit)}"
        action_type = "sell"
        amount_advice = sell_text(int(hold*0.15), cur, hold, unit)
    else:
        _down_line = f"  ▶ 🔴【D级】{quality_desc} → {sell_text(int(hold*0.2), cur, hold, unit)} | 破前低{low20}必须走"
        action_type = "sell"
        amount_advice = sell_text(int(hold*0.2), cur, hold, unit)
    
    # ===== V3.0 交易状态机裁决（decision_manager：优先级P0-P5+冷却+反转门槛+T+1） =====
    # 四重退出先行计算（P0强制退出/P2盈利保护 的触发输入）
    exit_trig, exit_lines = [], []
    if pos and pos.get("buy_price"):
        exit_lines, exit_trig = build_exit_plan(code, name, cur, kline, ma10, ma20, atr14, pos, no_sell=no_sell, unit=unit)
        if no_sell:
            # 套牢票禁清仓铁律（用户定死）：P0 强制退出类触发降级为警告——
            # 保留退出系统提示文本（position_manager 已按 no_sell 生成"禁清仓→做T"文案），
            # 但不把触发传给状态机（否则 P0 会输出"清仓"指令，违反禁清仓纪律）
            exit_trig = [t for t in exit_trig if t.get("kind") not in ("止损退出", "趋势退出清仓", "时间退出")]
    dm = decision_manager.finalize(
        code=code, name=name, raw_action=action_type, quality=quality, score=score,
        cur=cur, pos=pos, hold=hold, is_etf=is_etf, market_state=mstate,
        exit_triggers=exit_trig, can_buy=can_buy, rsi=rsi14,
    )
    final_action = dm["action"]  # BUY / ADD / HOLD / REDUCE / SELL
    if final_action in ("BUY", "ADD"):
        action_type = "buy"
    elif final_action in ("REDUCE", "SELL"):
        action_type = "sell"
    else:
        action_type = "hold"

    # ===== 操作点位 + 动作文本（按状态机最终动作输出，被拦的原始信号不打印） =====
    if action_type == "buy" and _up_line:
        lines.append(_up_line)
        if _up_stop:
            lines.append(_up_stop)
        if no_sell:
            # 套牢持仓：输出做T点位，不显示清仓/止损（用户定死不清仓）
            lines.append(f"  🎯 做T点位：高抛{res5:.3f}附近卖 / 回落{buy_zone[0]:.3f}附近接回（先卖后买）")
            ACTION_LIST.append({
                "name": name, "code": code, "action": "套牢做T", "amount": amount_advice,
                "buy": f"{buy_zone[0]:.3f}~{buy_zone[1]:.3f}",
                "sell": f"做T高抛{res5:.3f}附近/不清仓", "stop": "无(做T摊成本)",
            })
        elif buy_zone and sell_zone and stop_zone:
            lines.append(f"  🎯 买入点位：回踩{buy_zone[0]:.3f} ~ {buy_zone[1]:.3f}")
            if chase_zone:
                lines.append(f"  ⚡ 追涨点位：放量突破{chase_zone}追入(止损{buy_zone[0]:.3f}下方)")
            lines.append(f"  🎯 卖出点位：{sell_zone[0]:.3f} ~ {sell_zone[1]:.3f}")
            lines.append(f"  🛑 止损点位：{stop_zone:.3f}")
            ACTION_LIST.append({
                "name": name, "code": code, "action": "买入", "amount": amount_advice,
                "buy": f"{buy_zone[0]:.3f}~{buy_zone[1]:.3f}",
                "sell": f"{sell_zone[0]:.3f}~{sell_zone[1]:.3f}",
                "stop": f"{stop_zone:.3f}",
            })
    elif action_type == "sell" and _down_line:
        lines.append(_down_line)
        if sell_zone and stop_zone:
            lines.append(f"  🎯 卖出点位：{sell_zone[0]:.3f} ~ {sell_zone[1]:.3f}")
            lines.append(f"  🛑 止损点位：{stop_zone:.3f}")
        ACTION_LIST.append({
            "name": name, "code": code, "action": "减仓/卖出", "amount": amount_advice,
            "buy": "已持有", "sell": f"{sell_zone[0]:.3f}~{sell_zone[1]:.3f}" if sell_zone else "—",
            "stop": f"{stop_zone:.3f}" if stop_zone else "—",
        })

    # ===== 状态机输出（四段式：当前状态/状态变化/最终动作/下一触发） =====
    lines.extend(dm["lines"])
    if dm["can_sell_shares"] is not None and final_action in ("REDUCE", "SELL"):
        lines.append(f"  🔒 可卖份额≈{dm['can_sell_shares']}{unit}（今日买入{dm['today_bought']}{unit} T+1不可卖）")
    _trig_parts = []
    if pos and pos.get("buy_price"):
        if final_action in ("HOLD", "REDUCE") and not no_sell:
            if chase_zone and mstate not in ("C", "D"):
                _trig_parts.append(f"突破{chase_zone}再加仓")
            if ma20:
                _trig_parts.append(f"跌破MA20({ma20:.3f})减仓")
            if pos.get("stop_loss"):
                _trig_parts.append(f"破{float(pos.get('stop_loss')):.3f}清仓")
    else:
        if buy_zone:
            _trig_parts.append(f"回踩{buy_zone[0]:.3f}企稳买入")
        if chase_zone and mstate not in ("C", "D"):
            _trig_parts.append(f"放量突破{chase_zone}追入")
        if buy_zone:
            _trig_parts.append(f"破{round(buy_zone[0]*0.97,3):.3f}放弃")
    if _trig_parts:
        lines.append("  🎯 下一触发: " + " / ".join(_trig_parts))

    # ===== 四重退出系统（第五阶段）：仅对真实持仓输出（有成本价的持仓）=====
    if exit_lines:
        lines.extend(exit_lines)
        for _t in exit_trig:
            ACTION_LIST.append({
                "name": name, "code": code, "action": f"🚨{_t['kind']}", "amount": _t["label"],
                "buy": f"成本{pos.get('buy_price')}", "sell": "触发即走", "stop": "—",
            })

    # 记录信号用于复盘（自动写入signal_log.csv，v2.31：增加状态/策略版本字段）
    # V3.0：按状态机最终动作记录（被冷却/反转门槛拦成hold的不记，防信号噪音）；
    #       附加 old_action/new_action/change_reason（V3.0 第7节 决策变化原因）
    # V1.4（2026-08-12 体检修复）：四重退出触发（exit_trig）此前只进 ACTION_LIST 不进
    # SIGNAL_LOG → 08-07 起卖出信号 0 条（v2.31 卖出14条全在 08-05/06）。现退出触发
    # 也记"卖出/减仓"（套牢票 no_sell 的退出提示已在 1102-1106 过滤为做T类，仍记减仓
    # 信号供虚拟账户跟踪；P0 类清仓触发不会出现在套牢票上）。
    # V1.4（2026-08-12 体检）：历史脏数据防御——old/new_action 必须是标准动作标签，
    # 否则置空（08-05/06/07 曾把 code/name 写进动作字段污染3行，见 rule_audit7）
    _VALID_ACTIONS = ("买入", "加仓", "持有", "减仓", "清仓")
    _oa = decision_manager._ACTION_LABEL.get(dm["prev_action"], dm["prev_action"])
    _na = decision_manager._ACTION_LABEL.get(dm["action"], dm["action"])
    if _oa not in _VALID_ACTIONS:
        _oa = ""
    if _na not in _VALID_ACTIONS:
        _na = ""
    exit_signal = bool(exit_trig) and action_type != "buy"
    if action_type in ("buy", "sell") or exit_signal:
        SIGNAL_LOG.append({
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "code": code, "name": name,
            "action": "买入" if action_type == "buy" else "卖出/减仓",
            "price": cur, "grade": quality, "score": score,
            "industry": _industry_of(name, code),
            "mkt_score": MARKET.get("score"),
            "mkt_state": MARKET.get("state"),
            "status": "待执行",  # 虚拟交易跟踪状态：待执行/虚拟买入/虚拟卖出/已忽略
            "version": STRATEGY_VERSION,
            "old_action": _oa,
            "new_action": _na,
            "change_reason": "；".join(dm["change_reason"][:3]) or "",
        })

    # V1.5 输出过滤（2026-08-21 用户要求：4次定时分析输出太多）：
    # FILTER_MIN_GRADE=A 时，非持仓且等级≤B（B/C/D）的标的整块不输出
    # （不送AI分析、不推送QQ），日志照记。持仓/ETF核心池A/S级保留。
    # V1.6（2026-08-26）：股票池来源的票（from_pool=True）豁免过滤——
    # 用户要求股票池所有票定时/手动分析都逐只包含，不受等级过滤。
    if FILTER_MIN_GRADE == "A" and not pos and quality in ("B", "C", "D") and not from_pool:
        return ""

    # V1.6 尾盘确定性结论收集（2026-08-21 用户要求：尾盘14:45确定性买卖）
    # 每只标的必须落到六动作之一：建仓/加仓/减仓/清仓/持有/不买，禁止"观望/观察"模糊词。
    # 判定优先级：①四重退出触发(清仓/减仓) ②套牢no_sell(持有+做T) ③状态机最终动作 ④观察标的(建仓/不买)
    try:
        if CURRENT_PERIOD == "尾盘":
            if pos:  # 持仓：加仓/减仓/清仓/持有
                if exit_trig and not no_sell:
                    # 退出触发（止损/趋势/盈利保护/时间）→ 最严格者优先
                    _sev = max(exit_trig, key=lambda t: 0 if t.get("severity") == "high" else 1)
                    FINAL_LIST.append({"action": "清仓" if _sev.get("severity") == "high" else "减仓",
                                       "text": f"{_sev.get('label','')}（{_sev.get('kind','退出')}触发）"})
                elif no_sell:
                    FINAL_LIST.append({"action": "持有",
                                       "text": f"{lot_text(int(hold*0.15), cur, unit)}做T高抛低吸摊成本，禁清仓"})
                elif final_action in ("BUY", "ADD"):
                    FINAL_LIST.append({"action": "加仓", "text": (_up_line or f"加仓{amount_advice}")})
                elif final_action in ("REDUCE", "SELL"):
                    FINAL_LIST.append({"action": "清仓" if final_action == "SELL" else "减仓",
                                       "text": (_down_line or f"{sell_text(int(hold*0.2), cur, hold, unit)}")})
                else:
                    FINAL_LIST.append({"action": "持有", "text": f"现价{cur}持有不动，破{stop_zone if stop_zone else 'MA20'}再走"})
            else:  # 观察/候选：建仓或明确不买
                if quality in ("S", "A") and can_buy and not _gate:
                    FINAL_LIST.append({"action": "建仓",
                                       "text": f"现价{cur}买入{amount_advice}（吃明日溢价）"})
                elif quality in ("S", "A") and _gate:
                    FINAL_LIST.append({"action": "不买",
                                       "text": f"买点形态不佳({'；'.join(_gate)})，明日回踩{buy_zone[0] if buy_zone else '支撑'}再考虑"})
                elif quality in ("S", "A"):
                    FINAL_LIST.append({"action": "不买", "text": f"趋势未确认(未站稳MA20)，站稳{ma20 if ma20 else 'MA20'}再买"})
                elif quality == "B":
                    FINAL_LIST.append({"action": "不买", "text": "B级历史胜率不足(32-44%)，不建仓"})
                else:
                    FINAL_LIST.append({"action": "不买", "text": f"{quality}级偏空，破前低{low20}则放弃"})
    except Exception:
        pass  # 确定性结论收集失败不影响主流程

    return "\n".join(lines)


def main():
    global ACTION_LIST, CURRENT_PERIOD, FILTER_MIN_GRADE, FINAL_LIST
    ACTION_LIST = []
    FINAL_LIST = []  # V1.6 尾盘确定性结论（每标的六动作之一）
    # V1.5（2026-08-21 用户要求）：FILTER_MIN_GRADE=A 时非持仓B级及以下不输出
    # （short_term_ai.py 4次定时任务开启；手动分析/晚间持仓任务不设置=全量）
    FILTER_MIN_GRADE = os.environ.get("FILTER_MIN_GRADE", "")
    # HOLD_ONLY=1：只分析现有持仓（positions.json 权威），跳过观察清单/股票池/监测名单等非持仓标的
    HOLD_ONLY = os.environ.get("HOLD_ONLY") == "1"
    load_positions_map()  # 加载权威持仓（成本价/买入日期/固定止损）
    load_stock_pool()  # 股票池三路合并第1路（17:30五因子选股，date校验）
    load_watch_stocks()  # 次日监测名单（三路合并第3路，供股票池区块去重）
    
    period_map = {9: "早盘", 11: "收割后", 13: "午后", 14: "尾盘"}
    hour = datetime.now().hour
    period = "尾盘"
    for h, p in sorted(period_map.items(), reverse=True):
        if hour >= h: period = p; break
    CURRENT_PERIOD = period
    
    today = datetime.now().strftime("%Y-%m-%d")
    
    print(f"╔═══════════════════════════════╗")
    print(f"║  ⚡ 短线套利信号 {period}            ║")
    print(f"║  {today}                      ║")
    print(f"╚═══════════════════════════════╝")
    
    # 大盘
    print("\n📊 【大盘】")
    indices = get_indices()
    for name, d in indices.items():
        e = "🟢" if d["chg"]>=0 else "🔴"
        print(f"  {e} {name}: {d['chg']:+.2f}%")
    
    # 市场环境评分（总闸门：决定当日允许仓位）
    try:
        for _l in market_score(indices):
            print(_l)
    except Exception as _e:
        print(f"\n⚠️ 市场评分生成异常: {_e}")
    
    # 大盘近20日涨幅基准（RS相对强度对比用，默认上证指数）
    bench_chg20 = None
    try:
        _idx_k = get_index_kline("sh000001", 30)
        if _idx_k and len(_idx_k) >= 21:
            bench_chg20 = round((float(_idx_k[-1]["close"])/float(_idx_k[-21]["close"])-1)*100, 2)
    except Exception:
        pass
    # 沪深300近20日涨幅（相对强度第二基准：跑赢宽基）
    bench300_chg20 = None
    try:
        _idx_k = get_index_kline("sh000300", 30)
        if _idx_k and len(_idx_k) >= 21:
            bench300_chg20 = round((float(_idx_k[-1]["close"])/float(_idx_k[-21]["close"])-1)*100, 2)
    except Exception:
        pass
    
    # ETF 核心池（ETFS 全部输出，含未持仓的观察；hold=0 的按观察标的出信号）
    # HOLD_ONLY 模式：只保留实际持仓的ETF，其余不输出
    all_etfs = ETFS + EXTRA_ETFS
    if HOLD_ONLY:
        all_etfs = [e for e in all_etfs if e["code"] in POS_MAP]
    # 批量抓取换手率+主力资金（资金因子用，单次东财请求；含股票池core标的）
    try:
        if HOLD_ONLY:
            _em_codes = list(POS_MAP.keys())
        else:
            _pool_codes = [e.get("code") for e in STOCK_POOL.get("core", [])] if STOCK_POOL.get("valid") else []
            _em_codes = [e["code"] for e in all_etfs + WATCH_ETFS] + [c for c, *_ in STOCKS] + _pool_codes
        fetch_em_extra(_em_codes)
    except Exception:
        pass
    # V1.5 过滤：先收集，剔除空块（B级及以下非持仓），标题数量=实际输出数
    _etf_blocks = []
    for etf in all_etfs:
        _pos = POS_MAP.get(etf["code"])
        # 持仓金额以权威 POS_MAP 为准（与个股一致），防止硬编码 hold 过期
        _hold = _pos.get("amount", etf.get("hold", 0)) if _pos else etf.get("hold", 0)
        _r = analyze_item(etf["code"], etf["name"], _hold, total_amount=TOTAL_ETF, is_etf=True, bench_chg20=bench_chg20, bench300_chg20=bench300_chg20, no_sell=position_manager.is_trapped(etf["code"]), pos=_pos)
        if _r:
            _etf_blocks.append(_r)
    if _etf_blocks:
        print(f"\n📈 【ETF核心池 ({len(_etf_blocks)}只)】")
        print("="*55)
        for _b in _etf_blocks:
            print(_b)
    
    # 观察清单（HOLD_ONLY 模式下跳过——只分析现有持仓）
    if WATCH_ETFS and not HOLD_ONLY:
        _watch_blocks = []
        for etf in WATCH_ETFS:
            _pos = POS_MAP.get(etf["code"])
            _hold = _pos.get("amount", etf.get("hold", 0)) if _pos else etf.get("hold", 0)
            result = analyze_item(etf["code"], etf["name"], _hold, total_amount=TOTAL_ETF, is_etf=True, bench_chg20=bench_chg20, bench300_chg20=bench300_chg20, pos=_pos)
            if result:
                _watch_blocks.append((etf, result))
        if _watch_blocks:
            print(f"\n👀 【观察清单 ({len(_watch_blocks)}只)】")
            print("="*55)
        for etf, result in _watch_blocks:
            print(result)
            # 如果评分高，额外提示买入机会
            if "S级" in result or "A级" in result:
                print(f"  💡 {etf['name']}出现买入信号！可考虑建仓500~1000元")

    # ⚡ ETF T策略（持仓ETF日内高抛低吸降成本；引擎输出，AI解读不得改价）
    try:
        import etf_t_engine
        _t_held = [p for p in POS_MAP.values() if p.get("type") == "etf"
                   and (p.get("t_config") or {}).get("enable", True)]
        if _t_held:
            print(f"\n⚡ 【ETF T策略】日内做T降成本（高抛低吸，持仓不变）")
            print("=" * 55)
            for _p in _t_held:
                _t = etf_t_engine.analyze(_p["code"], _p.get("name", ""))
                if _t.get("error"):
                    print(f"  {_p.get('name')}({_p['code']}): ⚠️ {_t['error']}")
                    continue
                print(f"  {_t['name']}({_t['code']}) 状态:{_t['state']} 窗口:{_t.get('window','?')}({_t.get('time','')}) 今日最高:{_t['today_high_pct']:+.2f}% 当前:{_t['cur_pct']:+.2f}% 日内位置:{_t.get('day_pos','-')}% T评分:{_t['t_score']}")
                print(f"    操作: {_t['action']}")
                if _t.get('sell_zone'):
                    print(f"    卖出区: {_t['sell_zone']}（T仓{_t.get('shares', 0)}份）")
                if _t.get('buyback_zone'):
                    print(f"    回补区: {_t['buyback_zone']}")
                if _t.get('risk'):
                    print(f"    风险: {_t['risk']}")
                for _r in _t.get('reasons', []):
                    print(f"    - {_r}")
                if _t.get('pending_warn'):
                    print(f"    ⚠️ {_t['pending_warn']}")
                # V2.0 低吸T区块（引擎输出，AI 不得改价/手数）
                _d = _t.get("dip")
                if _d:
                    print(f"    ── 低吸T: {_d['state']} 评分:{_d['score']} 窗口:{_d.get('window','?')}")
                    print(f"    低吸操作: {_d['action']}")
                    if _d.get('buy_zone'):
                        print(f"    低吸买入区: {_d['buy_zone']}（{_d.get('shares', 0)}份）")
                    if _d.get('sell_zone'):
                        print(f"    低吸卖出区: {_d['sell_zone']}")
                    if _d.get('stop_zone'):
                        print(f"    低吸止损: {_d['stop_zone']}")
                    if _d.get('ref_buy_zone') and not _d.get('buy_zone'):
                        print(f"    📍 参考低吸区: {_d['ref_buy_zone']}（预案，评分未触发）")
                    for _r in _d.get('reasons', []):
                        print(f"      - {_r}")
    except Exception as _e:
        print(f"  [etf_t_engine 异常] {_e}")

    # 个股（HOLD_ONLY 模式：只输出实际持仓的股票）
    if STOCKS:
        _stocks = STOCKS
        if HOLD_ONLY:
            _stocks = [s for s in STOCKS if s[0] in POS_MAP]
        _stock_blocks = []
        for code, name, _ex, hold in _stocks:
            _pos = POS_MAP.get(code)
            if _pos:
                hold = _pos.get("amount", hold)  # 以权威持仓金额为准
            _r = analyze_item(code, name, hold, total_amount=TOTAL_STOCK, is_etf=False, bench_chg20=bench_chg20, bench300_chg20=bench300_chg20, pos=_pos)
            if _r:
                _stock_blocks.append(_r)
        if _stock_blocks:
            print(f"\n📈 【个股观察 ({len(_stock_blocks)}只, {TOTAL_STOCK}元)】")
            print("="*55)
            for _b in _stock_blocks:
                print(_b)

    # 🧺 股票池（stock_pool.py 五因子选股；V1.7 2026-08-26 筛选方案A：core 全部逐只 +
    # watch 综合分 top5 逐只，其余 watch 一行简略——用户嫌 V1.6 全量24只太臃肿；date 过期回退）
    # HOLD_ONLY 模式：跳过整个区块（只分析现有持仓）
    if STOCK_POOL.get("valid") and not HOLD_ONLY:
        _sp_core = STOCK_POOL.get("core", [])
        _sp_watch = STOCK_POOL.get("watch", [])
        # V1.7 筛选：watch 按综合分降序，前 WATCH_DETAIL_TOP 只逐只，其余一行简略
        _sp_watch_sorted = sorted(_sp_watch, key=lambda x: -(x.get("total_score") or 0))
        _sp_watch_detail = _sp_watch_sorted[:WATCH_DETAIL_TOP]
        _sp_watch_brief = _sp_watch_sorted[WATCH_DETAIL_TOP:]
        _sp_covered = {c for c, *_ in STOCKS} | {w.get("code", "") for w in WATCH_STOCKS}
        _pool_blocks, _pool_tags = [], []
        _brief_lines = []
        for _lvl, _lst in (("core", _sp_core), ("watch", _sp_watch_detail)):
            for _it in _lst:
                _code = _it.get("code", "")
                if not _code or _code in _sp_covered:
                    continue
                _sp_covered.add(_code)
                _name = _it.get("name", "")
                _pos = POS_MAP.get(_code)
                _r = analyze_item(_code, _name, 0, total_amount=TOTAL_STOCK, is_etf=False,
                                  bench_chg20=bench_chg20, bench300_chg20=bench300_chg20,
                                  pos=_pos, from_pool=True)
                if _r:
                    _pool_blocks.append(_r)
                    _pool_tags.append((_lvl, _it))
        # 其余 watch：一行简略（不逐只展开）
        for _it in _sp_watch_brief:
            _code = _it.get("code", "")
            if not _code or _code in _sp_covered:
                continue
            _sp_covered.add(_code)
            _brief_lines.append(
                f"  {_code} {_it.get('name','')} total={_it.get('total_score','-')} "
                f"{_it.get('industry','')}({_it.get('industry_score','-')}) 入池{_it.get('days_in_pool','-')}日")
        if _pool_blocks or _brief_lines:
            print(f"\n🧺 【股票池 ({len(_pool_blocks)}只逐只+{len(_brief_lines)}只简略, "
                  f"生成{STOCK_POOL.get('date','')} 市场{STOCK_POOL.get('market_status','')}级"
                  f"{STOCK_POOL.get('market_score','')}分)】")
            print("=" * 55)
            for _b, (_lvl, _it) in zip(_pool_blocks, _pool_tags):
                _up = "升core需≥85分" if _lvl == "watch" else "core"
                _tag = (f"  📌 股票池{_up}: 总分{_it.get('total_score','-')} 行业{_it.get('industry','')}"
                        f"({_it.get('industry_score','-')}分) 入池{_it.get('days_in_pool','-')}日 20日{_it.get('chg20','-')}%"
                        f" | 当日五因子选出,AI裁决进监测")
                print(_b + "\n" + _tag)
            if _brief_lines:
                print("  ── 其余 watch 简略（未逐只展开，可关注明日升core）──")
                for _l in _brief_lines:
                    print(_l)
    elif STOCK_POOL.get("date") and not HOLD_ONLY:
        print(f"\n⚠️ 股票池数据过期（生成于{STOCK_POOL.get('date','')}，间隔{STOCK_POOL.get('stale_days','-')}天，非最近交易日）→ 今日个股覆盖回退为固定自选+监测名单；下次定时将重新生成")
    elif not HOLD_ONLY:
        print("\n⚠️ 股票池文件缺失/不可读 → 今日个股覆盖回退为固定自选+监测名单；下次定时将重新生成")

    # 🎯 次日监测名单（18:00选股允许交易YES的标的，盘中重点跟踪；已由 main() 开头 load_watch_stocks 加载）
    # HOLD_ONLY 模式：跳过（只分析现有持仓）
    if WATCH_STOCKS and not HOLD_ONLY:
        _mon_blocks = []
        for w in WATCH_STOCKS:
            _code = w.get("code", "")
            _name = w.get("name", "")
            if not _code:
                continue
            _pos = POS_MAP.get(_code)
            _hold = _pos.get("amount", 0) if _pos else 0  # 已买入则按持仓显示
            _r = analyze_item(_code, _name, _hold, total_amount=TOTAL_STOCK, is_etf=False, bench_chg20=bench_chg20, bench300_chg20=bench300_chg20, pos=_pos)
            if _r:
                _mon_blocks.append(_r + f"\n  📌 监测来源：{w.get('reason','18:00选股')}（加入{w.get('added','')}；未买入则次日自动移出）")
        if _mon_blocks:
            print(f"\n🎯 【次日监测名单 ({len(_mon_blocks)}只)】")
            print("="*55)
            for _b in _mon_blocks:
                print(_b)
    elif STOCKS and not HOLD_ONLY:
        print("\n（今日无监测名单标的，18:00选股报告会生成）")
    
    # ⛔ 持仓退出检查（权威数据源 POS_MAP，含固定止损/四重退出，不允许止损下移）
    try:
        for _it in POS_MAP.values():
            _code, _stop = _it.get("code"), _it.get("stop_loss")
            if not _code or not _stop:
                continue
            _rt = get_rt(_code)
            if not _rt:
                continue
            if _rt["cur"] <= float(_stop):
                print(f"\n⛔ 【止损警报】{_it['name']}({_code}) 现价{_rt['cur']} 已跌破固定止损{_stop}！按纪律无条件卖出，不得再拖")
    except Exception:
        pass
    
    # 尾盘总结
    print(f"\n{'='*55}")
    print(f"💡 {period}总结")
    if period == "尾盘":
        print(f"  这是今天最后操作窗口，14:55前完成下单")
        print(f"  尾盘直接操作：看好就现价买吃明日溢价，不挂单不追尾盘急拉")
    elif period == "收割后":
        print(f"  量化收割结束，可观察捡漏")
        print(f"  但建议尾盘14:45再最终确认")
    elif period == "午后":
        print(f"  午后行情延续，观察方向确认")
        print(f"  尾盘14:45再做最终决策")
    else:
        print(f"  早盘趋势初显，观望为主")
        print(f"  关注突发消息对持仓的影响")
        print(f"  集合竞价已出：高开低开已定，挂单预案见各标的，勿追高开冲高")
    
    # 🧾 手续费硬性规则（用户2026-08-06要求：所有分析计划必须体现）
    print("\n🧾 【手续费硬性规则】佣金万2.5最低5元/笔(买卖双向) + 卖出印花税0.05%(个股) + 过户费万0.1")
    print("   单笔<3000元：手续费占比≥0.17%不划算 → 建议凑到3000元+整手(资金允许时)")
    print("   成本档位(买入): 1000元≈5.1元(0.51%) | 2000元≈5.2元(0.26%) | 3000元≈5.3元(0.18%) | 5000元≈5.5元(0.11%) | 10000元≈6.0元(0.06%)")
    print("   卖出个股另加0.05%印花税；做T一买一卖成本≥10元，价差必须覆盖，否则白做")

    print(f"\n⚠️ 仅供参考，投资有风险！")
    
    # ===== 尾盘风控报告 =====
    if period == "尾盘":
        try:
            # 收集所有持仓的当前价格（权威口径：POS_MAP，而非脚本内硬编码 hold——避免漏掉新买入标的）
            prices = {}
            for _code in POS_MAP:
                rt = get_rt(_code)
                if rt:
                    prices[_code] = rt["cur"]
            print(position_manager.generate_risk_report(prices))
            print(position_manager.generate_signal_stats_report())
            print(_position_health_report())  # V3.0 持仓健康评分（Phase 3）
        except Exception as e:
            print(f"\n⚠️ 风控报告生成异常: {e}")
    
    # 信号记录写入 signal_log.csv（自动复盘胜率用；v2.31 12字段 → V3.0 15字段含决策变化原因）
    # 表头升级独立于本次是否有信号：每次运行检查一次（12列→15列一次性升级，csv.DictReader 兼容旧行）
    try:
        import csv
        _sp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_log.csv")
        _headers = ["时间", "代码", "名称", "操作", "价格", "信号等级", "评分", "行业",
                    "市场评分", "市场状态", "状态", "策略版本", "旧动作", "新动作", "变化原因"]
        _new = not os.path.exists(_sp)
        if _new:
            with open(_sp, "w", newline="", encoding="utf-8") as _f:
                csv.writer(_f).writerow(_headers)
        else:
            with open(_sp, "r", newline="", encoding="utf-8") as _f:
                _all = _f.readlines()
            if _all and "变化原因" not in _all[0]:
                _all[0] = ",".join(_headers) + "\n"
                with open(_sp, "w", newline="", encoding="utf-8") as _f:
                    _f.writelines(_all)
    except Exception:
        pass
    if SIGNAL_LOG:
        try:
            import csv
            _sp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_log.csv")
            # V1.4（2026-08-12 体检修复）：同代码+同日期+同价格+同操作 已存在则跳过——
            # 之前每次运行（含手动/重复触发）都追加，08-04 单日单标的重复22次，
            # 复盘胜率被重复样本污染（136组重复）。读全量做去重集合。
            _seen = set()
            try:
                with open(_sp, "r", newline="", encoding="utf-8") as _f:
                    for _row in csv.reader(_f):
                        if len(_row) >= 5 and _row[0] != "时间":
                            _seen.add((_row[0], _row[1], _row[3], _row[4]))
            except Exception:
                pass
            _dedup = [_s for _s in SIGNAL_LOG
                      if (_s["date"], _s["code"], _s["action"], str(_s["price"])) not in _seen]
            _seen |= {(_s["date"], _s["code"], _s["action"], str(_s["price"])) for _s in _dedup}
            if _dedup:
                with open(_sp, "a", newline="", encoding="utf-8") as _f:
                    _w = csv.writer(_f)
                    for _s in _dedup:
                        _w.writerow([_s["date"], _s["code"], _s["name"], _s["action"], _s["price"],
                                     _s["grade"], _s["score"], _s.get("industry", ""),
                                     _s.get("mkt_score", ""), _s.get("mkt_state", ""),
                                     _s.get("status", "待执行"), _s.get("version", STRATEGY_VERSION),
                                     _s.get("old_action", ""), _s.get("new_action", ""),
                                     _s.get("change_reason", "")])
        except Exception:
            pass

if __name__ == "__main__":
    main()
