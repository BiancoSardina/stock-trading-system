"""Entry qualification independent of ranking; defaults are risk policy, not fitted alpha."""
import math
import os
from datetime import datetime
from paper_execution import fee, SLIPPAGE

VERSION = "v3.2.0"
MIN_NET_RR = float(os.environ.get("ENTRY_MIN_NET_RR", "1.5"))
STOP_COOLDOWN_SESSIONS = int(os.environ.get("STOP_COOLDOWN_SESSIONS", "2"))
if not math.isfinite(MIN_NET_RR) or MIN_NET_RR < 1 or STOP_COOLDOWN_SESSIONS < 1:
    raise ValueError("入场净盈亏比至少为1，止损观察期至少为1个完整交易日")


def positive(value):
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def economics(code, price, stop, target, quantity):
    """Compare net target profit with stopped loss, using the same cost model as paper fills."""
    result = {"allowed": False, "net_rr": None, "net_profit": None, "risk": None}
    if not all(positive(x) for x in (price, stop, target, quantity)):
        return dict(result, reason="缺少有效价格或交易数量")
    price, stop, target = map(float, (price, stop, target))
    if int(quantity) != float(quantity) or int(quantity) % 100:
        return dict(result, reason="交易数量不符合整手约束")
    if not stop < price < target:
        return dict(result, reason="必须满足止损价 < 入场价 < 参考目标")
    quantity = int(quantity)
    buy_gross = quantity * price * (1 + SLIPPAGE)
    target_gross = quantity * target * (1 - SLIPPAGE)
    stop_gross = quantity * stop * (1 - SLIPPAGE)
    buy_cost = buy_gross + fee(buy_gross, code)
    net_profit = target_gross - fee(target_gross, code, True) - buy_cost
    risk = buy_cost - (stop_gross - fee(stop_gross, code, True))
    rr = net_profit / risk if risk > 0 else None
    ok = rr is not None and rr >= MIN_NET_RR and net_profit > 0
    return {"allowed": ok, "net_rr": rr, "net_profit": net_profit, "risk": risk,
            "entry_cost": buy_cost, "roundtrip_fees": fee(buy_gross, code) + fee(target_gross, code, True),
            "slippage": SLIPPAGE, "min_net_rr": MIN_NET_RR,
            "reason": "成本与盈亏比通过" if ok else "扣除费用和滑点后目标收益不足或净盈亏比不达标"}


def make_plan(price, ma10, bars, today=None):
    """Use completed, observed prices only. Never move support to chase the current quote."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    hist = [b for b in bars if str(b.get("day", ""))[:10] < today
            and all(positive(b.get(k)) for k in ("low", "high", "close"))]
    hist = sorted({str(b['day'])[:10]: b for b in hist}.values(), key=lambda b: b['day'])
    if len(hist) < 5 or not positive(price):
        return None
    support = min(float(b['low']) for b in hist[-5:])
    if positive(ma10) and float(ma10) < price:
        support = max(support, float(ma10))
    # Preserve the existing 3% structural-stop distance; no historical optimization claim.
    stop = round(support * .97, 3)
    ceiling = round(support * 1.015, 3)
    resistance = max(float(b['high']) for b in hist[-5:])
    return {"support": round(support, 3), "entry_low": round(support, 3),
            "entry_high": ceiling, "stop": stop, "target": round(resistance, 3),
            "basis": "已完成日线近5日低点/MA10支撑与近5日高点；不制造突破后目标"}


def assess(code, quote, plan, quantity, grade, is_etf, ma5=None, now=None):
    reasons = []
    now = now or datetime.now()
    hm = now.strftime('%H:%M')
    if not ('09:30' <= hm <= '11:30' or '13:00' <= hm < '15:00'):
        reasons.append("连续交易时段外，仅生成预案")
    price = quote.get('cur')
    if not plan or not positive(price):
        return {"allowed": False, "reasons": reasons + ["缺少可核验的支撑和压力位"], "plan": plan}
    price = float(price)
    econ = economics(code, price, plan['stop'], plan['target'], quantity)
    if not plan['entry_low'] <= price <= plan['entry_high']:
        reasons.append("现价未进入结构支撑买入区，禁止上移买入区追价")
    if not econ['allowed']:
        reasons.append(econ['reason'])
    if grade not in ('S', 'A'):
        reasons.append("评分仅供排序，建仓或加仓仍需S/A资格")
    if not is_etf and grade == 'A':
        # A-stock samples were weak: require observable recovery, rather than changing the grade threshold.
        levels = (ma5, quote.get('open'), quote.get('prev'))
        if not all(positive(v) for v in levels) or price < max(float(v) for v in levels):
            reasons.append("A级个股尚未收复MA5、开盘价和昨收，维持观察")
    return {"allowed": not reasons, "reasons": reasons, "plan": plan, "economics": econ}


def cooldown_reason(last_stop_day, completed_dates, today):
    if not last_stop_day:
        return None
    try:
        datetime.strptime(last_stop_day, '%Y-%m-%d')
    except (ValueError, TypeError):
        return "止损观察期日期异常，暂停重新买入"
    if completed_dates is None:
        return "止损后缺少完整交易日数据，暂停重新买入"
    elapsed = len({str(d)[:10] for d in completed_dates if last_stop_day < str(d)[:10] < today})
    if elapsed < STOP_COOLDOWN_SESSIONS:
        return f"止损后观察期：已完成{elapsed}/{STOP_COOLDOWN_SESSIONS}个后续交易日，暂停买入/加仓"
    return None
