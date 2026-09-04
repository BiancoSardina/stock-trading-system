"""Deterministic hypothetical fills with settlement lots and explicit costs.

Signal prices are reference quotes, never claimed to be actual fills. Existing
v2 paper history is kept separate from this v3.1 account.
"""
import math
import os
from copy import deepcopy
from runtime import positive

SLIPPAGE = float(os.environ.get("PAPER_SLIPPAGE", "0.001"))
COMMISSION_RATE = float(os.environ.get("PAPER_COMMISSION_RATE", "0.00025"))
COMMISSION_MIN = float(os.environ.get("PAPER_COMMISSION_MIN", "5"))
STAMP_RATE = float(os.environ.get("PAPER_STAMP_RATE", "0.0005"))
TRANSFER_RATE = float(os.environ.get("PAPER_TRANSFER_RATE", "0.00001"))
if not 0 <= SLIPPAGE < 1 or any(not math.isfinite(v) or v < 0 for v in (COMMISSION_RATE, COMMISSION_MIN, STAMP_RATE, TRANSFER_RATE)):
    raise ValueError("模拟成交费用或滑点参数无效")


def fee(amount, code, sell=False):
    stock = str(code).startswith(("0", "3", "6"))
    return round(max(COMMISSION_MIN, amount * COMMISSION_RATE)
                 + (amount * TRANSFER_RATE if stock else 0)
                 + (amount * STAMP_RATE if stock and sell else 0), 2)


def refresh_position(pos):
    pos["shares"] = sum(lot["shares"] for lot in pos["lots"])
    pos["price"] = sum(lot["cost"] for lot in pos["lots"]) / pos["shares"] if pos["shares"] else 0


def buy(paper, sig, price):
    action = sig.get("new_action")
    if action not in ("买入", "加仓") or not positive(price) or sig.get("mkt_state") not in ("A", "B", "C"):
        return False
    code = sig["code"]
    existing = paper["positions"].get(code)
    if sig.get("grade") not in (("S", "A", "B") if existing and action == "加仓" else ("S", "A")):
        return False
    if action == "加仓" and (not existing or price <= existing["price"]):
        return False
    if action == "买入" and existing:
        return False
    fill = price * (1 + SLIPPAGE)
    # Cost valuation is explicit; this is a rule replay, not historical NAV.
    total = paper["cash"] + sum(p["shares"] * p["price"] for p in paper["positions"].values())
    current = (existing or {}).get("shares", 0) * fill
    invested = total - paper["cash"]
    market_cap = {"A": .8, "B": .6, "C": .3}[sig["mkt_state"]]
    budget = min(total * .05, max(0, total * .30 - current), paper["cash"],
                 max(0, total * market_cap - invested))
    shares = int(budget / fill / 100) * 100
    if sig.get('version') == 'v3.2.0':
        try:
            requested = int(sig.get('quantity') or 0)
        except (TypeError, ValueError):
            return False
        if requested <= 0 or requested % 100:
            return False
        shares = min(shares, requested)
    while shares > 0 and shares * fill + fee(shares * fill, code) > budget:
        shares -= 100
    if shares <= 0:
        return False
    if sig.get('version') == 'v3.2.0':
        from entry_policy import economics
        if not economics(code, price, sig.get('entry_stop'), sig.get('entry_target'), shares)['allowed']:
            return False
    gross = shares * fill
    fees = fee(gross, code)
    cost = gross + fees
    pos = paper["positions"].setdefault(code, {"name": sig.get("name", code), "lots": [],
            "grade": sig.get("grade"), "version": sig.get("version"), "buy_date": sig["date"]})
    pos["lots"].append({"date": sig["date"][:10], "shares": shares, "cost": cost,
                        "grade": sig.get("grade"), "version": sig.get("version")})
    refresh_position(pos)
    paper["cash"] = round(paper["cash"] - cost, 8)
    paper["trades"].append(dict(sig, action="虚拟买入", price=fill, shares=shares,
                                amount=round(gross, 2), fees=fees, fill_model="报价加滑点假设"))
    return True


def sell(paper, sig, price):
    if sig.get("new_action") not in ("清仓", "减仓") or not positive(price):
        return False
    code = sig["code"]
    pos = paper["positions"].get(code)
    if not pos:
        return False
    lots = pos["lots"]
    available = sum(lot["shares"] for lot in lots if lot["date"] < sig["date"][:10])
    if sig["new_action"] == "清仓":
        quantity = available
    else:
        try:
            ratio = float(sig.get("reduce_ratio") or 0)
        except (TypeError, ValueError):
            return False
        if not 0 < ratio < 1:
            return False
        quantity = min(available, int(pos["shares"] * ratio / 100) * 100)
    if quantity <= 0:
        return False
    fill = price * (1 - SLIPPAGE)
    fees = fee(quantity * fill, code, True)
    remaining = quantity
    for lot in lots:
        if lot["date"] >= sig["date"][:10] or not remaining:
            continue
        take = min(remaining, lot["shares"])
        allocated_cost = lot["cost"] * take / lot["shares"]
        allocated_fee = fees * take / quantity
        net = take * fill - allocated_fee
        paper["trades"].append(dict(sig, action="虚拟卖出", price=fill, shares=take,
            amount=round(take * fill, 2), fees=allocated_fee, grade=lot["grade"],
            version=lot["version"], pnl=round(net - allocated_cost, 2),
            pnl_pct=round((net / allocated_cost - 1) * 100, 2), fill_model="报价减滑点假设"))
        lot["shares"] -= take
        lot["cost"] -= allocated_cost
        remaining -= take
    pos["lots"] = [lot for lot in lots if lot["shares"]]
    refresh_position(pos)
    if not pos["shares"]:
        del paper["positions"][code]
    paper["cash"] = round(paper["cash"] + quantity * fill - fees, 8)
    return True
