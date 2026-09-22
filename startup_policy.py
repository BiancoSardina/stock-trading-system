"""Budget-independent research: early setups, not orders or fitted predictions.

Only completed daily bars define the setup. A current quote can invalidate or
trigger it, but cannot move its support/target. Defaults are testable hypotheses,
not a claim of improved returns. Execution remains owned by entry_policy.
"""
import math
from datetime import datetime

VERSION = "startup/v1"
MIN_SCORE = 60
MIN_PRICE_RR = 1.5
MAX_EXTENSION = .02
MAX_STOP_DISTANCE = .06


def positive(value):
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def completed_bars(bars, today):
    """Reject malformed/duplicate historical rows instead of silently filling them."""
    out = {}
    for row in bars:
        day = str(row.get("day", ""))[:10]
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            return []
        if day >= today:
            continue
        if day in out or not all(positive(row.get(k)) for k in ("open", "high", "low", "close", "volume")):
            return []
        b = {k: float(row[k]) for k in ("open", "high", "low", "close", "volume")}
        if not b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]:
            return []
        out[day] = dict(b, day=day)
    return [out[d] for d in sorted(out)]


def evaluate(bars, quote, market="UNKNOWN", now=None, benchmark=None):
    """Return candidate evidence and at most one conditional price plan, no size.

    Five factors: structure 25, stabilization 20, supply/volume 20,
    relative strength 15, location 20. Missing benchmark earns no RS points.
    """
    now = now or datetime.now()
    result = {"version": VERSION, "candidate": False, "setup": None, "score": 0,
              "factors": {}, "status": "数据不足", "plan": None, "triggered": False,
              "reasons": [], "limitations": ["日线与报价快照不证明分钟级企稳；不预测涨停",
                  "价格盈亏比未扣手续费及滑点；不含下单数量"], "analysis_only": True}
    hist = completed_bars(bars, now.strftime("%Y-%m-%d"))
    if len(hist) < 60 or not positive(quote.get("cur")):
        result["reasons"] = ["需要至少60根完整有效日线和有效报价"]
        return result
    if quote.get("date") != now.strftime("%Y-%m-%d"):
        result["reasons"] = ["报价日期缺失或不是本交易日"]
        return result
    if (now.date() - datetime.strptime(hist[-1]["day"], "%Y-%m-%d").date()).days > 7:
        result["reasons"] = ["最近完整日线超过7个自然日，需核验停牌/假期或数据缺失"]
        return result
    close = [b["close"] for b in hist]
    recent, prior = hist[-5:], hist[-10:-5]
    price = float(quote["cur"])
    support = min(b["low"] for b in recent)
    pivot = max(b["high"] for b in hist[-10:])
    prior_low = min(b["low"] for b in prior)
    atr = sum(max(hist[i]["high"]-hist[i]["low"], abs(hist[i]["high"]-hist[i-1]["close"]),
                  abs(hist[i]["low"]-hist[i-1]["close"])) for i in range(len(hist)-14, len(hist))) / 14
    ma5, old_ma5 = sum(close[-5:])/5, sum(close[-10:-5])/5
    ma20, old_ma20 = sum(close[-20:])/20, sum(close[-25:-5])/20
    width = (max(b["high"] for b in recent) / support - 1)
    old_width = max(b["high"] for b in prior) / prior_low - 1
    vol_ratio = sum(b["volume"] for b in recent) / sum(b["volume"] for b in prior)
    down = [b["volume"] for b in recent if b["close"] < b["open"]]
    up = [b["volume"] for b in recent if b["close"] >= b["open"]]
    supply_eases = vol_ratio <= 1.05 or (down and up and sum(down)/len(down) <= sum(up)/len(up))
    holds_low = support >= prior_low - .25 * atr
    recovering_low = hist[-1]["low"] >= min(b["low"] for b in hist[-3:-1]) and close[-1] > support + .25 * atr
    lower_steps = sum(recent[i]["low"] < recent[i-1]["low"] for i in range(1, 5))
    drifting_down = lower_steps >= 3 and close[-1] < close[-5]
    flattening = ma5 >= old_ma5 * .985 and close[-1] >= close[-3] * .985
    compressed = width <= .08 and width <= max(old_width * 1.1, .035)
    drawdown = close[-1] / max(b["high"] for b in hist[-60:]) - 1
    setup = "超跌企稳型" if drawdown <= -.08 else "整理启动型"
    historical_strength = ma20 >= old_ma20 * .995
    rs = rs_previous = None
    benchmark = benchmark or {}
    dates = [hist[i]['day'] for i in (-1, -6, -11)]
    if all(positive(benchmark.get(d)) for d in dates):
        index_now, index5, index10 = (float(benchmark[d]) for d in dates)
        rs = (close[-1]/close[-6] - index_now/index5)*100
        rs_previous = (close[-6]/close[-11] - index5/index10)*100
    else:
        result["limitations"].append("缺少同日期基准日线，相对强度因子不加分")
    extension = price / pivot - 1
    stop = math.floor((support - max(atr * .25, support * .005))*100) / 100
    distance = (price-stop)/price
    factors = {
        "trend": 10*int(holds_low) + 10*int(compressed) + 5*int(historical_strength),
        "momentum": 10*int(flattening) + 10*int(recovering_low),
        "capital": 10*int(bool(supply_eases)) + 10*int(vol_ratio <= 1.2 and compressed),
        "rs": (10*int(rs >= 0) + 5*int(rs > rs_previous)) if rs is not None else 0,
        "risk": 10*int(0 < distance <= MAX_STOP_DISTANCE) + 10*int(extension <= MAX_EXTENSION),
    }
    reasons = []
    if not (holds_low and flattening and compressed) or drifting_down:
        reasons.append("低点/重心或整理结构未稳定，缩量阴跌不算企稳")
    if not supply_eases:
        reasons.append("抛压尚未减弱")
    if price <= stop:
        reasons.append("跌破结构失效位")
    if extension > MAX_EXTENSION or distance > MAX_STOP_DISTANCE:
        reasons.append("现价已涨远或距失效位过远")
    score = sum(factors.values())
    if score < MIN_SCORE:
        reasons.append("启动机会评分不足")
    result.update(score=score, factors=factors, setup=setup, candidate=not reasons,
                  status="提前候选" if not reasons else "不符合形态", reasons=reasons,
                  evidence={"as_of": hist[-1]["day"], "support": round(support, 3),
                            "pivot": round(pivot, 3), "stop": stop, "atr": round(atr, 4),
                            "width_pct": round(width*100, 2), "volume_ratio_5v5": round(vol_ratio, 3),
                            "drawdown_pct": round(drawdown*100, 2), "rs5": rs,
                            "previous_rs5": rs_previous})
    if not result["candidate"]:
        return result
    # One path only. Breakout targets must be pre-existing overhead prices.
    breakout = price > pivot
    if breakout:
        overhead = [b["high"] for b in hist[:-10] if b["high"] > pivot + .5 * atr]
        target = min(overhead) if overhead else None
        low = math.ceil((pivot + .01)*100)/100
        high = min(pivot*(1+MAX_EXTENSION), pivot+.5*atr)
        mode = "首次突破"
    else:
        target = pivot
        low = math.ceil((support+.25*atr)*100)/100
        high = support+.75*atr
        mode = "支撑附近转强"
    if target is not None and stop > 0:
        high = math.floor(min(high, (target + MIN_PRICE_RR*stop)/(1+MIN_PRICE_RR))*100)/100
        if low <= high and stop < low < target:
            result["plan"] = {"mode": mode, "entry_low": low, "entry_high": high,
                              "stop": stop, "target": round(target, 3),
                              "min_price_rr": MIN_PRICE_RR,
                              "price_rr": round((target-price)/(price-stop), 3) if price>stop else None,
                              "basis": "仅已完成日线；区间上沿受价格盈亏比限制，非净盈亏比",
                              "valid_on": now.strftime("%Y-%m-%d")}
    gates = []
    if market not in ("A", "B", "C"):
        gates.append("市场D级禁买" if market == "D" else "市场状态未知，禁止新增买入")
    if breakout and market == "C":
        gates.append("市场C级仅低吸，不做突破追涨")
    if now.weekday() >= 5 or not ("09:30" <= now.strftime("%H:%M") < "11:30" or "13:00" <= now.strftime("%H:%M") < "15:00"):
        gates.append("非连续交易时段，仅生成预案")
    if not result["plan"]:
        gates.append("历史压力空间不足，暂无合格买入区间")
    elif not low <= price <= high:
        gates.append("现价不在条件买入区间，不随现价上移区间")
    levels = (quote.get("open"), quote.get("prev"), ma5)
    if not all(positive(v) for v in levels) or price < max(float(v) for v in levels if positive(v)):
        gates.append("尚未收复开盘价、昨收和已完成日线MA5，等待转强")
    if not positive(quote.get("low")) or float(quote["low"]) <= stop:
        gates.append("当日低点缺失或已触及失效位，取消本轮触发")
    result["triggered"] = not gates
    result["status"] = "早期条件满足" if not gates else "候选待确认"
    result["reasons"] = gates or ["形态、位置与报价转强条件满足；仅研究建议，不是订单"]
    return result


BUY_STATES = ("条件满足", "等待", "失效")
INVALID_MARK = "失效位"


def _off_session(now):
    return (now.weekday() >= 5
            or not ("09:30" <= now.strftime("%H:%M") < "11:30"
                    or "13:00" <= now.strftime("%H:%M") < "15:00"))


def buy_state_record(result, now=None):
    """买点状态（时点量）——与"值得关注程度"分离的独立字段。

    条件满足：本轮全部触发闸门通过；
    失效：形态本身不成立，或当日已触及结构失效位；
    等待：形态成立但闸门未过（未到区间/未收复开盘·昨收·MA5/非交易时段等）。
    质量分类（core/watch）不得随本字段变化。
    """
    now = now or datetime.now()
    reasons = list(result.get("reasons") or [])
    if not result.get("candidate"):
        state = "失效"
    elif result.get("triggered"):
        state = "条件满足"
    elif any(INVALID_MARK in str(x) for x in reasons):
        state = "失效"
    else:
        state = "等待"
    off = _off_session(now)
    if state == "条件满足":
        note = "本轮闸门全过"
    elif state == "失效":
        note = "结构失效位被跌破或形态不成立：本轮不形成买点结论"
    elif off:
        note = "非交易时段评估，仅生成预案；买点为时点量，下一交易时段复核"
    else:
        note = "盘中评估：闸门未过，等待触发"
    return {"state": state, "reasons": reasons, "note": note, "off_session": bool(off),
            "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
            "time_independent_note": "买点状态随时点变化；core/watch 分类只看形态质量与行业"}


def render(result):
    """Research results must never be rendered as broker instructions."""
    f = result["factors"]
    lines = [f"  🌱 启动机会：{result['setup'] or '未识别'} | {result['status']} | 机会评分{result['score']}/100（非涨停概率）"]
    if f:
        lines.append(f"  🧮 机会五因子：结构{f['trend']}/25 企稳{f['momentum']}/20 抛压{f['capital']}/20 相对强度{f['rs']}/15 位置{f['risk']}/20")
    evidence = result.get("evidence")
    if evidence:
        lines.append(f"  形态证据截至{evidence['as_of']}：近5日振幅{evidence['width_pct']:.2f}%；5日均量/前5日均量{evidence['volume_ratio_5v5']:.2f}；结构低点{evidence['support']:.2f}、整理上沿{evidence['pivot']:.2f}")
    p = result.get("plan")
    if p:
        lines.append(f"  📍 条件买入区间({p['mode']})：{p['entry_low']:.2f}~{p['entry_high']:.2f}；失效位{p['stop']:.2f}；参考压力{p['target']:.2f}；有效日{p['valid_on']}")
        lines.append(f"  条件：区间内收复开盘价、昨收及已完成日线MA5，当日未触及失效位；价格盈亏比≥{MIN_PRICE_RR:g}（未扣费）")
    lines.append("  判定：" + "；".join(result["reasons"]))
    _state = buy_state_record(result)
    lines.append(f"  🔔 买点状态：{_state['state']}｜{_state['note']}")
    lines.append("  限制：" + "；".join(result["limitations"]))
    return lines
