#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""decision_manager V3.0 单测（临时文件，不污染生产 decision_state.json）"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import decision_manager as dm

# 重定向到临时文件
_tmp = tempfile.mkdtemp(prefix="dm_test_")
dm.STATE_FILE = os.path.join(_tmp, "decision_state.json")
dm.HISTORY_FILE = os.path.join(_tmp, "decision_history.json")
dm._STATES = None
dm._today_bought_shares = lambda code: 0  # 默认无当日买入

PASS, FAIL = 0, 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

def base_pos(code="600111", buy_price=10.0, amount=10000, stop_loss=8.0, buy_date="2026-08-01"):
    return {"code": code, "name": "测试股", "buy_price": buy_price, "amount": amount,
            "quantity": 1000, "stop_loss": stop_loss, "buy_date": buy_date, "type": "stock"}

def now_minus(minutes):
    return datetime.now() - timedelta(minutes=minutes)

print("== 1. 建仓（P5 买入） ==")
r = dm.finalize("600111", "测试股", "buy", quality="S", score=88, cur=10.5, pos=None,
                hold=0, is_etf=False, market_state="A", exit_triggers=[], can_buy=True)
check("S级建仓→BUY", r["action"] == "BUY", r["action"])
check("状态→BUYING", r["state"] == "BUYING", r["state"])
check("无持仓时不输出可卖", r["can_sell_shares"] is None)

print("== 2. 建仓强度不足（B级不建仓，需S/A） ==")
r = dm.finalize("600112", "测试股2", "buy", quality="B", score=70, cur=10.5, pos=None,
                hold=0, is_etf=False, market_state="A", exit_triggers=[], can_buy=True)
check("B级→HOLD", r["action"] == "HOLD", r["action"])

print("== 3. 冷却期（BUY后10分钟再同信号→维持，不重复喊） ==")
r = dm.finalize("600113", "测试股3", "buy", quality="S", score=90, cur=10.5, pos=None,
                hold=0, is_etf=False, market_state="A", exit_triggers=[], can_buy=True,
                now=now_minus(1))  # 第一次
check("首信号→BUY", r["action"] == "BUY", r["action"])
r = dm.finalize("600113", "测试股3", "buy", quality="S", score=90, cur=10.6, pos=None,
                hold=0, is_etf=False, market_state="A", exit_triggers=[], can_buy=True,
                now=now_minus(0))  # 1分钟后重复
check("10分钟内重复→HOLD(冷却)", r["action"] == "HOLD", r["action"])
check("冷却原因记录", any("冷却" in x for x in r["change_reason"]), str(r["change_reason"]))

print("== 4. 亏损持仓禁止加仓 ==")
r = dm.finalize("600114", "亏损股", "buy", quality="A", score=80, cur=9.0, pos=base_pos("600114", buy_price=10.0),
                hold=9000, is_etf=False, market_state="A", exit_triggers=[], can_buy=True)
check("亏损加仓→HOLD", r["action"] == "HOLD", r["action"])
check("亏损禁加原因", any("亏损" in x for x in r["change_reason"]), str(r["change_reason"]))

print("== 5. 盈利持仓加仓放行（P4） ==")
r = dm.finalize("600115", "盈利股", "buy", quality="A", score=82, cur=10.8, pos=base_pos("600115", buy_price=10.0),
                hold=10800, is_etf=False, market_state="A", exit_triggers=[], can_buy=True)
check("盈利加仓→ADD", r["action"] == "ADD", r["action"])

print("== 6. 反转门槛：刚加仓后弱回落（A→B降1级）→维持 ==")
r = dm.finalize("600115", "盈利股", "sell", quality="B", score=68, cur=10.6, pos=base_pos("600115", buy_price=10.0),
                hold=11000, is_etf=False, market_state="A", exit_triggers=[], can_buy=True,
                now=now_minus(0))
check("弱回落→HOLD(不反转)", r["action"] == "HOLD", r["action"])
check("反转门槛原因", any("反转" in x for x in r["change_reason"]), str(r["change_reason"]))

print("== 7. 反转门槛放行：刚加仓后重大恶化（A→D降3级）→允许减仓 ==")
r = dm.finalize("600115", "盈利股", "sell", quality="D", score=40, cur=9.8, pos=base_pos("600115", buy_price=10.0),
                hold=11000, is_etf=False, market_state="A", exit_triggers=[], can_buy=True,
                now=now_minus(0))
check("重大恶化→SELL", r["action"] == "SELL", r["action"])

print("== 8. P0 强制退出（跌破硬止损，趋势再好也压不住） ==")
trig = [{"kind": "止损退出", "severity": "high", "label": "清仓", "code": "600116", "name": "破位股"}]
r = dm.finalize("600116", "破位股", "buy", quality="S", score=95, cur=7.5, pos=base_pos("600116", buy_price=10.0, stop_loss=8.0),
                hold=7500, is_etf=False, market_state="A", exit_triggers=trig, can_buy=True)
check("S级+破止损→SELL(P0)", r["action"] == "SELL", r["action"])
check("状态→EXIT", r["state"] == "EXIT", r["state"])

print("== 9. 盈利保护态（PROTECT）禁止加仓 ==")
pos = base_pos("600117", buy_price=10.0)
pos["quantity"] = 1000
r = dm.finalize("600117", "保护股", "hold", quality="S", score=90, cur=12.0, pos=pos,
                hold=12000, is_etf=False, market_state="A", exit_triggers=[], can_buy=True)
check("盈利≥10%→PROTECT态", r["state"] == "PROTECT", r["state"])
r = dm.finalize("600117", "保护股", "buy", quality="S", score=90, cur=12.2, pos=pos,
                hold=12200, is_etf=False, market_state="A", exit_triggers=[], can_buy=True,
                now=now_minus(0))
check("PROTECT态加仓→HOLD", r["action"] == "HOLD", r["action"])

print("== 10. T+1：当日买入份额不可卖 ==")
def fake_today_bought(code):
    return 1100  # 当日买入1100股
dm._today_bought_shares = fake_today_bought
pos = base_pos("600118", buy_price=10.0, buy_date=datetime.now().strftime("%Y-%m-%d"))
pos["quantity"] = 2000  # 总2000股，今日买1100 → 可卖900
pos["lots"] = [dict(pos, quantity=900, buy_date="2026-08-01"), dict(pos, quantity=1100)]
r = dm.finalize("600118", "T1股", "sell", quality="C", score=55, cur=9.8, pos=pos,
                hold=19600, is_etf=False, market_state="A", exit_triggers=[], can_buy=True,
                now=now_minus(0))
check("C级减仓→REDUCE", r["action"] == "REDUCE", r["action"])
check("可卖份额=900", r["can_sell_shares"] == 900, str(r["can_sell_shares"]))
# 全部当日买入 → 不可减
pos2 = dict(pos); pos2["quantity"] = 1100; pos2.pop("lots", None)
r = dm.finalize("600118", "T1股", "sell", quality="D", score=40, cur=9.5, pos=pos2,
                hold=10450, is_etf=False, market_state="A", exit_triggers=[], can_buy=True,
                now=now_minus(0))
check("全为当日买入→HOLD(T+1)", r["action"] == "HOLD", r["action"])
dm._today_bought_shares = lambda code: 0

print("== 11. 状态校准：持仓被卖出后 HOLD→WATCH ==")
dm._STATES = None
r = dm.finalize("600119", "离场股", "hold", quality="C", score=50, cur=9.0, pos=base_pos("600119"),
                hold=9000, is_etf=False, market_state="A", exit_triggers=[], can_buy=False)
check("有持仓→HOLD", r["state"] == "HOLD", r["state"])
r = dm.finalize("600119", "离场股", "hold", quality="C", score=50, cur=9.0, pos=None,
                hold=0, is_etf=False, market_state="A", exit_triggers=[], can_buy=False)
check("已卖出→WATCH", r["state"] == "WATCH", r["state"])
check("上次动作记SELL", r["prev_action"] == "SELL", r["prev_action"])

print("== 12. 市场D级总闸门：buy降级 ==")
r = dm.finalize("600120", "闸门股", "buy", quality="A", score=80, cur=10.5, pos=None,
                hold=0, is_etf=False, market_state="D", exit_triggers=[], can_buy=True)
check("D级建仓→HOLD", r["action"] == "HOLD", r["action"])

print("== 13. 历史记录写入 ==")
hist = dm.load_history()
check("decision_history 有记录", len(hist) > 0, f"len={len(hist)}")
if hist:
    e = hist[-1]
    check("历史含 old/new action", "old_action" in e and "new_action" in e, str(e.get("new_action")))
    check("历史含 change_reason", bool(e.get("change_reason")), str(e.get("change_reason")))

print("== 14. 减仓重复最小间隔（60分钟防抖） ==")
r = dm.finalize("600121", "防抖股", "sell", quality="C", score=55, cur=9.8, pos=base_pos("600121"),
                hold=9800, is_etf=False, market_state="A", exit_triggers=[], can_buy=True,
                now=now_minus(1))
check("首次C级→REDUCE", r["action"] == "REDUCE", r["action"])
r = dm.finalize("600121", "防抖股", "sell", quality="C", score=54, cur=9.7, pos=base_pos("600121"),
                hold=9700, is_etf=False, market_state="A", exit_triggers=[], can_buy=True,
                now=now_minus(0))
check("10分钟内重复减仓→HOLD", r["action"] == "HOLD", r["action"])

print("== 15. 减仓后冷静期：120分钟内不加回 ==")
r = dm.finalize("600122", "冷静股", "sell", quality="C", score=55, cur=9.6, pos=base_pos("600122", buy_price=9.5),
                hold=9600, is_etf=False, market_state="A", exit_triggers=[], can_buy=True,
                now=now_minus(1))
check("先减仓→REDUCE", r["action"] == "REDUCE", r["action"])
r = dm.finalize("600122", "冷静股", "buy", quality="A", score=80, cur=10.2, pos=base_pos("600122", buy_price=9.5),
                hold=10200, is_etf=False, market_state="A", exit_triggers=[], can_buy=True,
                now=now_minus(0))
check("冷静期内加仓→HOLD", r["action"] == "HOLD", r["action"])
check("冷静期原因", any("冷静" in x for x in r["change_reason"]), str(r["change_reason"]))

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
