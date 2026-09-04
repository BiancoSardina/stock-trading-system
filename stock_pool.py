#!/usr/bin/env python3
"""
股票池V1.3 — 主程序（stock_pool.py）
====================================
每日 17:30 运行：全流程串行 8-12 分钟
  ① 市场状态（复用 short_term.market_score）
  ② 全市场拉取 + 基础过滤（stock_scanner）
  ③ 行业映射 + 行业强度评分（industry_rank，不再硬过滤）
  ④ 候选=全量基础过滤股票（附行业标签；--fast 按成交额粗筛前N只）
  ⑤ 全量五因子评分（复用 short_term 五因子函数 + 新浪K线，K线加间隔防456限流）
  ⑥ 位置风险修正（rise20/偏离MA20/RSI）+ 位置硬排除（>30%涨幅/偏离>12% 出局）
  ⑦ 行业准入（<40排除；40-55仅watch且需个股≥75）—— 行业"不拖后腿"
  ⑧ 综合排序 + 生命周期 + 池生成（V1.2：个股底线+趋势硬条件+行业配额，容量上限不填满）
  ⑨ 输出 stock_pool.json

V1.2 核心（设计见 stock_pool_design_v2.md）：
  评分目标修正：找全市场最强个股（个股0.85），行业只做约束（0.15权重 + 准入线 + 配额），
  不再是"最强行业里最强股票"（V1.1 行业硬过滤≥70 导致 core 被单行业垄断）。

V1.3 收紧（2026-08-10 用户诊断"池里全是涨多的票"）：
  · 位置硬排除 50%/20% → 30%/12%
  · 位置扣分起扣线 30%/20% → 15%/6%（15-25%扣5 / 25-35%扣10 / >35%扣15）
  · 动量/RS 因子 chg20 涨幅加分 6分封顶 → 3分封顶（_f_momentum/_f_rs 加 chg20_cap 参数，
    默认6=盘中分析行为不变，股票池传3）

V1.3.1 CORE_STRONG 强者通道（2026-08-10 用户方案：B/C弱市空池兜底）：
  · 普通池 B/C 市门槛（80/85）收紧后易空池 → 开精品通道：前置强条件全过（个股≥85/行业≥65/
    RS≥15/资金≥18/趋势成立）才享综合分 78 门槛（不享受宽容期）；D市禁买不开
  · 容量≤3（占 core 总量8内，单行业配额同普通core），level="strong" 标记（stock_pool_ai
    裁决输入已显示 level 字段，下游零改动）
  · 降级=条件不满足自动回落普通池判断（RS 回到15/行业65才能再入，天然缓冲）

用法：
  python3 stock_pool.py            # 完整生成（全量评分 ~10分钟）
  python3 stock_pool.py --limit 100  # 快速测试（只评分前N只）
  python3 stock_pool.py --fast      # 降级：按成交额粗筛前400只精评（~5分钟）
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import short_term
from runtime import macd, positive, exclusive, data_path, quote_is_fresh, weekly_averages, align_daily_bars
import stock_scanner
import industry_rank
import stock_pool_manager as spm

# ===== 评分常量（V1.2：评分目标=全市场最强个股，行业"不拖后腿"；设计见 stock_pool_design_v2.md）=====
SCORE_W_STOCK = 0.85        # 综合评分：个股权重（V1.1 0.7 → 个股主导）
SCORE_W_INDUSTRY = 0.15     # 综合评分：行业权重（V1.1 0.3 → 行业只微调）
IND_ELIMINATE = 40          # 行业准入：<40 排除（行业拖后腿，短线不做）
IND_CORE_MIN = 55           # 行业准入：<55 只能进 watch（不能进 core）
IND_WATCH_STRONG = 75       # 行业40-55 的票进 watch 需个股五因子≥75（弱行业里必须强者）
CORE_MAX_PER_IND = 2        # 行业配额：core 单行业最多2只（防垄断）
WATCH_MAX_PER_IND = 3       # 行业配额：watch 单行业最多3只
CORE_STOCK_MIN = 70         # 个股底线：core 需个股五因子≥70（A级）
WATCH_STOCK_MIN = 60        # 个股底线：watch 需个股五因子≥60（B级）
WATCH_TOTAL_MIN = 65        # watch 综合分底线（设计文档v1：watch 65≤总分<core门槛；2026-08-12修复空池bug）
POS_EXCLUDE_RISE20 = 30     # 位置硬排除：20日涨幅>30% 直接出局（V1.3收紧，原50%放行太多高位票）
POS_EXCLUDE_DIST = 12       # 位置硬排除：偏离MA20>12% 直接出局（原20%）
KLINE_SLEEP = 0.05          # K线请求间隔（秒，全量781只防456限流）
FAST_PRESCREEN = 400        # --fast 模式：实时行情粗筛前N只再精评（降级用）
MAX_POS_DEDUCT = 30         # 位置扣分上限
CORE_AI_LIMIT = 8           # 18:00 AI 裁决 core 前N只（与 stock_pool_ai.py 一致；core不足N只则全裁）

# ===== CORE_STRONG 强者通道（V1.3.1 2026-08-10 用户方案：C市空池兜底，弱市精选极强龙头）=====
# 设计要点：普通池 C 市 85 分门槛在 V1.3 收紧后容易空池 → 开一条"前置强条件全过才享低综合分"的精品通道。
# 降级=条件不满足自动回落普通池判断（RS 需回到 15 / 行业回到 65 才能再入，天然有缓冲）。
STRONG_MARKET = ("B", "C")  # 开启市场：B/C弱市都开（2026-08-10 实测 B 市也空池；D市禁买不开；A市门槛低不需要）
STRONG_STOCK_MIN = 85       # 个股五因子≥85（V1.3压档口径，等效旧版92）
STRONG_IND_MIN = 65         # 行业≥65（普通core≥55；弱市需行业资金支持）
STRONG_RS_MIN = 15          # RS≥15（V1.3收紧档满分17：跑赢上证>10%+跑赢300>5%+20日微涨）
STRONG_CAPITAL_MIN = 18     # 资金≥18（全量评分口径=量能8+换手4~6+流动性4~6，主力资金缺失）
STRONG_TOTAL_MIN = 78       # 综合分（扣分后）≥78（普通C市85；前置强条件全过才享78）
STRONG_MAX = 3              # 容量≤3（弱市不抱太多；占core总量8之内，仍受单行业≤2配额）

# ===== 位置风险修正（V1.1 修改二，total 层扣分，不进五因子）=====
# V1.3 收紧（2026-08-10 用户诊断：池里全是涨多的票）：起扣线 30%→15%、20%→6%，与硬排除线(30/12)衔接
def position_deduct(rise20, dist_ma20, rsi14):
    """
    返回 (扣分, 明细列表)
      rise20 15-25%→扣5；25-35%→扣10；>35%→扣15（>30 已被硬排除，档位为冗余保险）
      dist_ma20 6-10%→扣5；>10%→扣10（>12 已被硬排除）
      rsi14 70-80→扣5；>80→扣10
    """
    deduct, reasons = 0, []
    if rise20 is not None:
        if rise20 > 35:
            deduct += 15
            reasons.append(f"20日涨幅{rise20:.1f}%>35%扣15")
        elif rise20 > 25:
            deduct += 10
            reasons.append(f"20日涨幅{rise20:.1f}%>25%扣10")
        elif rise20 > 15:
            deduct += 5
            reasons.append(f"20日涨幅{rise20:.1f}%>15%扣5")
    if dist_ma20 is not None:
        if dist_ma20 > 10:
            deduct += 10
            reasons.append(f"偏离MA20 {dist_ma20:.1f}%>10%扣10")
        elif dist_ma20 > 6:
            deduct += 5
            reasons.append(f"偏离MA20 {dist_ma20:.1f}%>6%扣5")
    if rsi14 is not None:
        if rsi14 > 80:
            deduct += 10
            reasons.append(f"RSI{rsi14:.0f}>80扣10")
        elif rsi14 > 70:
            deduct += 5
            reasons.append(f"RSI{rsi14:.0f}>70扣5")
    return min(deduct, MAX_POS_DEDUCT), reasons


def _get_kline(code, days=120):
    """个股日K线（quotes.sina.cn 端点——实测连续20次无456限流；money.finance 端点会限流）"""
    pref = "sh" if code.startswith(("60", "68")) else "sz"
    url = (f"https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
           f"?symbol={pref}{code}&scale=240&ma=no&datalen={days}")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore"))
    except Exception:
        return []


def score_stock(code, name, ind_score, bench_chg20, bench300_chg20, amount=None, turnover=None):
    """
    单只股票五因子评分 + 位置修正，返回完整条目 dict（或 None）。
    复用 short_term 的 get_rt/calc_*/_f_* 函数，口径与盘中分析一致（K线用抗限流端点）。
    amount/turnover：当日成交额/换手率（来自基础过滤），用于资金因子。
    """
    try:
        rt = short_term.get_rt(code)
    except Exception:
        return None
    if not rt:
        return None
    try:
        kline = align_daily_bars(_get_kline(code, 120), rt)
        closes = [float(k["close"]) for k in kline]
        highs = [float(k["high"]) for k in kline]
        lows = [float(k["low"]) for k in kline]
        vols = [int(k["volume"]) for k in kline]
    except Exception:
        return None
    if len(closes) < 60 or not all(positive(x) for x in closes + highs + lows):
        return None

    if not quote_is_fresh(rt) or not positive(rt.get("cur")) or not positive(rt.get("prev")):
        return None
    cur = rt["cur"]
    chg = round((cur - rt["prev"]) / rt["prev"] * 100, 2) if rt.get("prev") else 0.0
    ma5 = round(sum(closes[-5:]) / 5, 3) if len(closes) >= 5 else None
    ma10 = round(sum(closes[-10:]) / 10, 3) if len(closes) >= 10 else None
    ma20 = round(sum(closes[-20:]) / 20, 3) if len(closes) >= 20 else None
    ma60 = short_term.calc_ma(closes, 60)
    ma60_slope = None
    if len(closes) >= 65:
        ma60_prev = sum(closes[-65:-5]) / 60
        ma60_slope = round((ma60 - ma60_prev) / ma60_prev * 100, 2)
    rsi14 = short_term.calc_rsi(closes)
    atr14 = short_term.calc_atr(highs, lows, closes, 14)
    atr_pct = round(atr14 / cur * 100, 2) if atr14 and cur else None
    chg20 = round((cur / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else None
    rs20 = round(chg20 - bench_chg20, 2) if chg20 is not None and bench_chg20 is not None else None
    rs20_300 = round(chg20 - bench300_chg20, 2) if chg20 is not None and bench300_chg20 is not None else None

    dif, dea, _hist = macd(closes)

    wma5, wma10 = weekly_averages(kline)
    bm = round(sum(closes[-20:]) / 20, 3) if len(closes) >= 20 else None

    hm = datetime.now().hour * 60 + datetime.now().minute
    elapsed = min(max(hm - 570, 0), 120) + min(max(hm - 780, 0), 120)
    progress = min(1., max(elapsed / 240, .05))
    avgv = sum(vols[-6:-1]) / 5
    vr = round(vols[-1] / progress / avgv, 2) if avgv > 0 else 1

    # 20日最大回撤
    max_dd20 = None
    if len(closes) >= 20:
        _peak, _dd = closes[-20], 0.0
        for _c in closes[-20:]:
            _peak = max(_peak, _c)
            _dd = min(_dd, (_c / _peak - 1) * 100)
        max_dd20 = round(_dd, 2)

    # 五因子（复用生产函数，口径一致）
    f_trend, _ = short_term._f_trend(cur, ma10, ma20, ma60, ma60_slope, bm, wma5, wma10)
    f_mom, _ = short_term._f_momentum(rsi14, dif, dea, chg20, chg20_cap=3)  # V1.3: 涨幅加分压档
    f_fund, _ = short_term._f_fund(vr, chg, {"turnover": turnover}, False)
    # V1.1 修改四：流动性评分并入资金因子（20日成交额 >10亿=6分/5-10亿=5/2-5亿=4，<2亿已被基础过滤淘汰）
    liq_extra = 0
    if amount is not None:
        liq_extra = 6 if amount > 10e8 else 5 if amount > 5e8 else 4
    f_fund = min(20, f_fund + liq_extra)
    f_rs, _ = short_term._f_rs(rs20, rs20_300, chg20, chg20_cap=3)  # V1.3: 涨幅加分压档
    f_risk, _ = short_term._f_risk(atr_pct, max_dd20)
    stock_score = min(100, f_trend + f_mom + f_fund + f_rs + f_risk)
    level, _ = short_term.score_grade(stock_score)

    # 位置风险修正
    rise20 = chg20  # rise20 = 20日涨幅（同 chg20）
    dist_ma20 = round((cur / ma20 - 1) * 100, 2) if ma20 else None
    # V1.2 位置硬排除（防高位接盘）：命中直接出局，不再进池（原V1.1仅扣分）
    if rise20 is not None and rise20 > POS_EXCLUDE_RISE20:
        return {"code": code, "name": name,
                "_exclude": f"20日涨幅{rise20:.1f}%>{POS_EXCLUDE_RISE20}%（高位）"}
    if dist_ma20 is not None and dist_ma20 > POS_EXCLUDE_DIST:
        return {"code": code, "name": name,
                "_exclude": f"偏离MA20 {dist_ma20:.1f}%>{POS_EXCLUDE_DIST}%（高位）"}
    deduct, pos_reasons = position_deduct(rise20, dist_ma20, rsi14)

    total = round(stock_score * SCORE_W_STOCK + ind_score * SCORE_W_INDUSTRY - deduct, 1)

    return {
        "code": code, "name": name,
        "stock_score": stock_score, "level": level,
        "total_score": total,
        "factor": {"trend": f_trend, "momentum": f_mom, "capital": f_fund,
                   "rs": f_rs, "risk": f_risk},
        "position": {"rise20": round(rise20, 2) if rise20 is not None else None,
                     "distance_ma20": dist_ma20,
                     "rsi14": round(rsi14, 1) if rsi14 is not None else None,
                     "deduct": deduct, "reasons": pos_reasons},
        "trend": {"above_ma20": bool(ma20 and cur > ma20),
                  "above_ma60": bool(ma60 and cur > ma60),
                  "ma20_gt_ma60": bool(ma20 and ma60 and ma20 > ma60)},
        "chg20": chg20, "price": cur,
        "levels": {"ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
                   "ma60_slope": ma60_slope, "atr14": atr14,
                   "support": min(lows[-20:]), "resistance": max(highs[-20:]),
                   "stop": max(ma20 or 0, cur - 2 * (atr14 or 0))},
        "quote_time": rt.get("date", "") + " " + rt.get("time", ""),
    }


def build_reasons(entry, ind_name, ind_score):
    """生成 reason 列表（供复盘）"""
    r = []
    if ind_score >= 70:
        r.append(f"行业强({ind_score}分)")
    if entry["trend"]["above_ma20"] and entry["trend"]["ma20_gt_ma60"]:
        r.append("趋势成立")
    if entry["factor"]["rs"] >= 12:
        r.append("RS领先")
    if entry["factor"]["trend"] >= 20:
        r.append("趋势分高")
    for x in entry["position"].get("reasons", []):
        r.append(x)
    return r


def generate_pool(scored, market_status, old_pool, today):
    """
    池生成（V1.2 重写）：
      · 规模（上限不填满）：A12/20 B10/18 C8/16 D0/8 —— 容量是最高限制，宁缺毋滥
      · 门槛：A75/B80/C85/D90（宽容期 days≤3 → -3）
      · 个股底线：core 需 stock_score≥70(A级)；watch 需 ≥60(B级)
      · 趋势硬条件：core 需 价>MA20 且 MA20>MA60；watch 需 价>MA20
      · 行业准入：<40 已被 main 排除；40-55(_watch_only) 只能进 watch；≥55 可进 core
      · 行业配额：core 单行业≤2 / watch 单行业≤3（防单一行业垄断）
      · 淘汰：total<70 / 行业<40 / 破MA60（旧池股票）
      · 升级/降级由排序自然实现（总分降序前N进core）
    返回 (core_pool, watch_pool, stats)
    """
    cap_core, cap_watch = spm.pool_capacity(market_status)
    lcmap = spm.old_lifecycle_map(old_pool)
    stats = {"evicted": [], "new": 0, "kept": 0}

    # 生命周期继承 + 淘汰
    entries = []
    for e in scored:
        code = e["code"]
        old_lc = lcmap.get(code)
        if old_lc:
            days = int(old_lc["days_in_pool"]) + int(old_lc.get("last_evaluated") != today)
            first_seen = old_lc.get("first_seen") or today
            evict, why = spm.evict_check(e["total_score"], e.get("industry_score"),
                                         e["trend"]["above_ma60"])
            if evict:
                stats["evicted"].append(f"{code} {e['name']}: {why}")
                continue
            stats["kept"] += 1
            is_old = True
        else:
            days, first_seen = 1, today
            stats["new"] += 1
            is_old = False
        e["first_seen"] = first_seen
        e["days_in_pool"] = days
        e["last_evaluated"] = today
        e["industry_score"] = e.get("industry_score", 0)
        e["_old"] = is_old
        entries.append(e)

    # 按总分降序
    entries.sort(key=lambda x: -x["total_score"])

    core_pool, watch_pool = [], []
    core_ind_count, watch_ind_count = {}, {}
    n_strong = 0  # CORE_STRONG 强者通道计数（C市≤3）
    # core 严格门槛（对齐降级线：core 跌破即降 watch，故入 core 必须≥严格门槛；宽容票只能进 watch）
    # V1.3.3（2026-08-12 用户调整）：C级 85→82（C弱市普通票门槛过高易空池，微调3分；强者通道78仍兜底）
    core_min = {"A": 75, "B": 80, "C": 82, "D": 90}.get(market_status, 80)
    for e in entries:
        ind_name = e.get("industry", "") or "?"
        f = e.get("factor", {})
        # CORE_STRONG 强者通道（V1.3.1，仅C市）：弱市精选极强龙头，防空池。
        # 前置强条件全硬性（不享受宽容期）：个股≥85 + 行业≥65 + RS≥15 + 资金≥18 + 趋势成立
        strong_pre = (market_status in STRONG_MARKET
                      and e["stock_score"] >= STRONG_STOCK_MIN
                      and e.get("industry_score", 0) >= STRONG_IND_MIN
                      and f.get("rs", 0) >= STRONG_RS_MIN
                      and f.get("capital", 0) >= STRONG_CAPITAL_MIN
                      and bool(e["trend"]["above_ma20"]) and bool(e["trend"]["ma20_gt_ma60"]))
        # V1.3.2 总门槛修复（2026-08-12）：原统一用市场门槛(如B级80)在 can_core/can_watch 之前
        # continue，导致 65~79 分的 watch 候选被误杀 → watch 长期空池。现总门槛只拦
        # 低于 watch 底线(65)的票；core/strong 各自门槛在分支内检查（core 分支 total>=core_min，
        # strong 分支 total>=STRONG_TOTAL_MIN 见下）。
        if e["total_score"] < WATCH_TOTAL_MIN:
            continue  # 低于 watch 底线(65)，出局
        # V1.2 从严：core 需 行业≥55 + 个股A级 + 趋势成立(价>MA20且MA20>MA60)
        can_core = (not e.get("_watch_only")
                    and e["stock_score"] >= CORE_STOCK_MIN
                    and e.get("industry_score", 0) >= IND_CORE_MIN
                    and bool(e["trend"]["above_ma20"]) and bool(e["trend"]["ma20_gt_ma60"]))
        # watch 需 个股B级 + 价>MA20
        can_watch = (e["stock_score"] >= WATCH_STOCK_MIN
                     and bool(e["trend"]["above_ma20"]))
        if strong_pre and e["total_score"] >= STRONG_TOTAL_MIN \
           and len(core_pool) < cap_core and n_strong < STRONG_MAX \
           and core_ind_count.get(ind_name, 0) < CORE_MAX_PER_IND:
            # 强者通道优先（总量仍受 cap_core 约束，单行业配额同普通core）
            e["level"] = "strong"
            core_pool.append(e)
            n_strong += 1
            core_ind_count[ind_name] = core_ind_count.get(ind_name, 0) + 1
        elif can_core and len(core_pool) < cap_core and e["total_score"] >= core_min \
             and core_ind_count.get(ind_name, 0) < CORE_MAX_PER_IND:
            e["level"] = "core"
            core_pool.append(e)
            core_ind_count[ind_name] = core_ind_count.get(ind_name, 0) + 1
        elif can_watch and len(watch_pool) < cap_watch \
             and watch_ind_count.get(ind_name, 0) < WATCH_MAX_PER_IND:
            watch_pool.append(e)
            watch_ind_count[ind_name] = watch_ind_count.get(ind_name, 0) + 1
        # 配额满/条件不满足 → 继续看下一只（不 break：配额按行业，其他行业仍可进）

    # 清理内部字段（不落盘）
    for grp in (core_pool, watch_pool):
        for e in grp:
            e.pop("_old", None)
            e.pop("_watch_only", None)

    return core_pool, watch_pool, stats


@exclusive(lambda: data_path("stock_pool.run"))
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只评分前N只（测试用）")
    ap.add_argument("--force-build", action="store_true", help="强制重建行业映射表（默认用当天缓存）")
    ap.add_argument("--fast", action="store_true",
                    help="降级模式：按成交额粗筛前N只精评（全量评分超时/限流风险时用）")
    args = ap.parse_args()

    t0 = time.time()
    today = datetime.now().strftime("%Y-%m-%d")

    # ① 市场状态（复用生产 market_score——返回 lines，dict 在全局 MARKET）
    try:
        short_term.market_score()
        m = short_term.MARKET or {}
        market_score_val = m.get("score")
        market_status = m.get("state", "UNKNOWN")
        if market_status not in ("A", "B", "C", "D") or not m.get("data_ok", False):
            raise ValueError("市场数据不完整")
    except Exception as exc:
        raise RuntimeError("市场评分不可用，保留旧股票池并停止后续发布") from exc
    print(f"[stock_pool] 市场状态: {market_status}级({market_score_val}分)", file=sys.stderr, flush=True)

    # ② 全市场 + 基础过滤
    all_stocks = stock_scanner.fetch_all_stocks()
    if not stock_scanner.LAST_FETCH_COMPLETE:
        raise RuntimeError("全市场数据不完整，保留旧池")
    kept, dropped = stock_scanner.basic_filter(all_stocks)
    print(f"[stock_pool] 全市场{len(all_stocks)} → 基础过滤后{len(kept)}只 "
          f"(剔除:{json.dumps(dropped, ensure_ascii=False)})", file=sys.stderr, flush=True)
    if not kept:
        print("[stock_pool] ❌ 基础过滤后为空，中止")
        sys.exit(1)

    # ③ 行业映射 + 行业评分（V1.2：不再硬过滤，改为后置准入约束）
    imap = industry_rank.build_industry_map(force=args.force_build)
    ind_scores = industry_rank.score_industries(all_stocks, imap)
    ind_index = industry_rank.stock_industry_index(imap)
    print(f"[stock_pool] 行业评分完成: {len(ind_scores)}个行业", file=sys.stderr, flush=True)

    # ④ V1.2 候选=全量基础过滤股票（股票先评分，行业后置约束），仅附行业标签
    candidates = []
    for k in kept:
        ind_name = ind_index.get(k["code"], "")
        ind_score = ind_scores.get(ind_name, {}).get("score", 0)
        k["industry"] = ind_name
        k["industry_score"] = ind_score
        candidates.append(k)
    candidates.sort(key=lambda x: -x["amount"])
    if args.fast:
        # --fast 降级模式：按成交额粗筛前N只精评（全量超时/限流风险时用）
        candidates = candidates[:FAST_PRESCREEN]
        print(f"[stock_pool] --fast 降级模式：按成交额粗筛前{len(candidates)}只精评", file=sys.stderr, flush=True)
    elif args.limit:
        candidates = candidates[:args.limit]
    print(f"[stock_pool] 全量五因子评分目标 {len(candidates)}只（V1.2：股票先排序，行业后过滤）",
          file=sys.stderr, flush=True)

    # ⑤ 基准指数（上证/沪深300 近20日涨幅）
    bench_chg20 = bench300_chg20 = None
    try:
        k1 = short_term.get_index_kline("sh000001", 30)
        k3 = short_term.get_index_kline("sh000300", 30)
        if k1 and len(k1) >= 21:
            bench_chg20 = round((float(k1[-1]["close"]) / float(k1[-21]["close"]) - 1) * 100, 2)
        if k3 and len(k3) >= 21:
            bench300_chg20 = round((float(k3[-1]["close"]) / float(k3[-21]["close"]) - 1) * 100, 2)
    except Exception:
        pass

    # ⑥ 五因子评分（串行K线拉取，约0.6s/只；V1.2 全量评分，位置硬排除在此统计）
    scored, fail, pos_excluded = [], 0, []
    for i, c in enumerate(candidates):
        e = score_stock(c["code"], c["name"], c["industry_score"], bench_chg20, bench300_chg20,
                        amount=c.get("amount"), turnover=c.get("turnover"))
        if e:
            if e.get("_exclude"):
                pos_excluded.append(f"{e['code']} {e['name']}: {e['_exclude']}")
                continue
            e["industry"] = c["industry"]
            e["industry_score"] = c["industry_score"]
            scored.append(e)
        else:
            fail += 1
        time.sleep(KLINE_SLEEP)  # V1.2 防限流（全量781只连续请求）
        if (i + 1) % 100 == 0:
            print(f"[stock_pool] 评分进度 {i+1}/{len(candidates)}", file=sys.stderr, flush=True)
    print(f"[stock_pool] 评分完成: 成功{len(scored)} 失败{fail} 位置硬排除{len(pos_excluded)}",
          file=sys.stderr, flush=True)

    if fail or bench_chg20 is None or bench300_chg20 is None:
        raise RuntimeError(f"候选行情或基准缺失（失败{fail}），保留旧池")

    # ⑦ V1.2 行业准入（行业"不拖后腿"：<40 排除；40-55 仅 watch 且需个股≥75）
    indu_excluded = []
    after = []
    for e in scored:
        ind = e.get("industry_score", 0)
        if ind < IND_ELIMINATE:
            indu_excluded.append(f"{e['code']} {e['name']}: 行业{ind}<{IND_ELIMINATE}")
            continue
        if ind < IND_CORE_MIN:
            e["_watch_only"] = True  # 行业中等：只能进 watch
            if e["stock_score"] < IND_WATCH_STRONG:
                indu_excluded.append(f"{e['code']} {e['name']}: 行业{ind}中等但个股{int(e['stock_score'])}<{IND_WATCH_STRONG}")
                continue
        after.append(e)
    scored = after
    print(f"[stock_pool] 行业准入排除 {len(indu_excluded)}只（<{IND_ELIMINATE} 或 行业中等但个股弱）",
          file=sys.stderr, flush=True)

    # ⑧ 池生成（生命周期 + 市场状态规模 + 行业配额/个股底线/趋势硬条件）
    total_scored = len(scored)  # 行业准入后数量（日报统计用）
    for e in scored:
        e["reason"] = build_reasons(e, e.get("industry", ""), e.get("industry_score", 0))
    old_pool = spm.load_old_pool()
    core_pool, watch_pool, stats = generate_pool(scored, market_status, old_pool, today)

    # ⑧ 输出 stock_pool.json
    out = {
        "date": today,
        "market_status": market_status,
        "market_score": market_score_val,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_ok": True, "source_count": len(all_stocks), "scored_count": len(candidates),
        "core_pool": core_pool,
        "watch_pool": watch_pool,
    }
    if args.limit:
        print("测试范围结果不写入正式股票池")
    else:
        spm.save_pool(out)
    print(f"[stock_pool] ✅ stock_pool.json 已生成 耗时{time.time()-t0:.0f}s", file=sys.stderr, flush=True)

    # ===== 日报（stdout → no_agent 投递）=====
    cap_core, cap_watch = spm.pool_capacity(market_status)
    if os.getenv("PIPELINE_COMPACT") == "1":
        print(f"[stock_pool] 摘要留后台: {market_status}{market_score_val} "
              f"CORE={len(core_pool)} WATCH={len(watch_pool)}", file=sys.stderr)
        return
    print(f"📋 股票池日报 {today} | 市场{market_status}级({market_score_val}分)")
    print(f"候选链路: 全市场{len(all_stocks)} → 主板过滤{len(kept)} → 全量五因子评分{total_scored}只"
          f"（位置硬排除{len(pos_excluded)} 行业准入排除{len(indu_excluded)}）")
    print(f"──────────────────────────────")
    if core_pool:
        print(f"🎯 CORE 核心池 {len(core_pool)}只（容量上限{cap_core}，筛选从严宁缺毋滥；"
              f"门槛{spm.entry_threshold(market_status, None)}）:")
        for e in core_pool:
            print(f"  {e['code']} {e['name']} total={e['total_score']} 个股{e['stock_score']} "
                  f"{e['industry']}({e['industry_score']}) 位置扣{e['position']['deduct']} "
                  f"d{e['days_in_pool']}天")
    else:
        print(f"🎯 CORE 核心池: 无达标票（门槛{spm.entry_threshold(market_status, None)}分"
              f"+ 个股≥{CORE_STOCK_MIN} + 趋势成立 + 行业≥{IND_CORE_MIN}）")
    if watch_pool:
        print(f"👀 WATCH 观察池 {len(watch_pool)}只（容量上限{cap_watch}）:")
        for e in watch_pool[:15]:
            print(f"  {e['code']} {e['name']} total={e['total_score']} 个股{e['stock_score']} "
                  f"{e['industry']}({e['industry_score']}) d{e['days_in_pool']}天")
    elif market_status != "D":
        print(f"👀 WATCH 观察池: 无达标票（个股≥{WATCH_STOCK_MIN} + 价>MA20）")
    # V1.2 行业分布（覆盖率软指标）
    from collections import Counter
    ind_core = Counter(e["industry"] for e in core_pool)
    ind_watch = Counter(e["industry"] for e in watch_pool)
    if ind_core or ind_watch:
        all_ind = set(ind_core) | set(ind_watch)
        dist = " ".join(
            f"{i}{ind_core.get(i, 0) + ind_watch.get(i, 0)}"
            for i in sorted(all_ind, key=lambda x: -(ind_core.get(x, 0) + ind_watch.get(x, 0)))
        )
        print(f"🧩 行业分布: {dist}（覆盖{len(all_ind)}个行业；core单行业≤{CORE_MAX_PER_IND}）")
    if stats["evicted"]:
        print(f"🗑 淘汰 {len(stats['evicted'])}只: " + "; ".join(stats["evicted"][:5]))
    print(f"──────────────────────────────")
    print(f"次日 18:00 AI 将裁决 CORE {len(core_pool)}只 → 监测名单；WATCH 直接进盘中观察")


if __name__ == "__main__":
    main()
