#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decision_manager.py — 交易状态机 + 决策优先级 + 信号稳定（V3.0 Phase 1，2026-08-07）
================================================================================
解决"几分钟叫加仓、几分钟叫减半"的信号抖动问题（用户 V3.0 架构方案落地）：

1. 每标的持久状态机（decision_state.json）：
   WATCH(观察) → READY(准备) → BUYING(买入信号) → HOLD(持仓) → PROTECT(盈利保护) → EXIT(退出)
2. 决策优先级 P0-P5（代码级强制，AI 只解释不推翻）：
   P0 强制退出 > P1 风险降低 > P2 保护利润 > P3 持有 > P4 加仓 > P5 买入
   —— 趋势评分再高，跌破硬止损也只能卖出（北方稀土案例）
3. 信号稳定（防抖三件套）：
   - 同向冷却：买入信号30分钟 / 加仓信号60分钟，冷却期内重复 → "维持"，不重复喊单
   - 反转门槛：刚加仓/买入后出现弱回落 → 不立刻反向减仓，需命中 P0/P1/重大变化
     （等级降≥2级 或 评分降≥15分）；刚减仓/清仓后 120 分钟冷静期内不加回
   - 减仓重复最小间隔60分钟（防减仓信号自身抖动；破硬止损不受限）
4. T+1 约束：当日买入份额当日不可卖（事实源=positions.json 中 buy_date==今天 的条目），
   减仓建议只针对可卖份额，可卖为0时减仓降级为"明日处理"
5. 决策变化原因：最终动作与上次不一致 → change_reason → decision_history.json
6. 状态校准铁律：每次决策前以 positions.json 为唯一事实源校准——
   事实有持仓 → HOLD 系；事实无持仓 → 回 WATCH；决策状态永远向持仓事实看齐

接入：short_term.py analyze_item() 尾部调用 finalize()，返回最终动作
（BUY/ADD/HOLD/REDUCE/SELL）覆盖原 action_type；定时/手动两个入口自动生效。
"""
import json
import os
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "decision_state.json")
HISTORY_FILE = os.path.join(SCRIPT_DIR, "decision_history.json")
HISTORY_LIMIT = 500  # 历史最多保留条数（防无限膨胀）

# 状态机六态
WATCH, READY, BUYING, HOLD, PROTECT, EXIT = "WATCH", "READY", "BUYING", "HOLD", "PROTECT", "EXIT"
# 最终动作
ACT_BUY, ACT_ADD, ACT_HOLD, ACT_REDUCE, ACT_SELL = "BUY", "ADD", "HOLD", "REDUCE", "SELL"
_ACTION_LABEL = {ACT_BUY: "买入", ACT_ADD: "加仓", ACT_HOLD: "持有", ACT_REDUCE: "减仓", ACT_SELL: "清仓"}

# 信号稳定参数（分钟）
COOLDOWN_BUY = 30            # 买入信号冷却
COOLDOWN_ADD = 60            # 加仓信号冷却
COOLDOWN_REDUCE_REPEAT = 60  # 同标的减仓重复最小间隔（破硬止损不受限）
COOLDOWN_REVERSE = 120       # 刚减仓/清仓后，反向(买入/加仓)的冷静期
SCORE_CHANGE_MAJOR = 15      # 评分降≥15分 = 重大变化
GRADE_DROP_MAJOR = 2         # 等级降≥2级 = 重大变化

_STATES = None  # 模块级缓存


# ─────────────────────────── 文件 IO ───────────────────────────

def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_states():
    global _STATES
    if _STATES is None:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            _STATES = data if isinstance(data, dict) else {}
        except Exception:
            _STATES = {}
    return _STATES


def save_states(states):
    global _STATES
    _STATES = states
    try:
        _atomic_write(STATE_FILE, states)
    except Exception:
        pass


def load_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def append_history(entry):
    try:
        hist = load_history()
        hist.append(entry)
        _atomic_write(HISTORY_FILE, hist[-HISTORY_LIMIT:])
    except Exception:
        pass


# ─────────────────────────── 状态工具 ───────────────────────────

def _new_state(code, name, pos):
    buy_price = float(pos.get("buy_price") or 0) if pos else 0
    return {
        "code": code, "name": name,
        "state": HOLD if pos else WATCH,
        "last_action": ACT_HOLD,
        "last_action_time": "",
        "cost": buy_price,
        "highest_price": buy_price,
        "today_bought": 0,  # 当日买入份额（缓存；事实以 positions.json buy_date 为准）
        "date": datetime.now().strftime("%Y-%m-%d"),
        "grade": "", "score": 0,
    }


def _fmt(now):
    return now.strftime("%Y-%m-%d %H:%M")


def _minutes_between(t1, t2):
    """两个 'YYYY-MM-DD HH:MM' 时间戳的分钟差；解析失败返回大数（视为冷却已过）"""
    try:
        d1 = datetime.strptime(t1, "%Y-%m-%d %H:%M")
        d2 = datetime.strptime(t2, "%Y-%m-%d %H:%M")
        return (d2 - d1).total_seconds() / 60.0
    except Exception:
        return 99999.0


def _today_bought_shares(code):
    """当日买入份额（T+1 事实源：positions.json 里 buy_date==今天 的条目份额合计）"""
    try:
        import position_manager
        data = position_manager.load_positions()
        today = datetime.now().strftime("%Y-%m-%d")
        total = 0.0
        for grp in ("etf", "stock"):
            for p in data.get(grp, []):
                if p.get("code") == code and str(p.get("buy_date", "")) == today:
                    q = p.get("shares") or p.get("quantity") or 0
                    if not q:
                        bp = float(p.get("buy_price") or 0)
                        q = round(float(p.get("amount") or 0) / bp) if bp else 0
                    total += float(q or 0)
        return int(total)
    except Exception:
        return 0


# ─────────────────────────── 核心裁决 ───────────────────────────

def finalize(code, name, raw_action, quality="", score=0, cur=0.0, pos=None,
             hold=0, is_etf=True, market_state="A", exit_triggers=None,
             can_buy=True, rsi=None, now=None):
    """V3.0 状态机最终裁决（每标的每次分析调用一次）。

    参数：
      raw_action    analyze_item 原始意图：buy / sell / hold / watch
      quality       信号等级 S/A/B/C/D
      score         五因子评分 0~100
      cur           现价
      pos           positions.json 该标的持仓条目（None=未持仓）——唯一事实源
      hold          持仓金额（元）
      market_state  市场状态 A/B/C/D（D=总闸门关闭）
      exit_triggers position_manager.build_exit_plan 返回的 triggered 列表（P0/P2 输入）
      can_buy       趋势闸门（现价>MA20 且 周线非空）——analyze_item 已算好
      rsi           RSI14（减仓一级提示：≥80 超买过热只提示不动手）

    返回 dict：
      state / prev_action / action(BUY|ADD|HOLD|REDUCE|SELL) / changed /
      change_reason / lines(状态机输出文本) / can_sell_shares(T+1可卖份额)
    """
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    states = load_states()
    st = states.get(code)
    if not st or not isinstance(st, dict):
        st = _new_state(code, name, pos)
    st["name"] = name
    if st.get("date") != today:
        st["today_bought"] = 0
        st["date"] = today

    # ── 0) 持仓事实校准（铁律：决策状态向 positions.json 看齐） ──
    pos_exist = bool(pos and pos.get("buy_price"))
    if pos_exist:
        st["cost"] = float(pos.get("buy_price") or st.get("cost") or 0)
        if st["state"] in (WATCH, READY, BUYING):
            st["state"] = HOLD  # 挂单/信号已成交 → 事实有持仓
    else:
        if st["state"] in (HOLD, PROTECT, EXIT):
            st["state"] = WATCH  # 事实已卖出 → 回观察
            st["last_action"] = ACT_SELL
            st["last_action_time"] = _fmt(now)
        elif st["state"] == BUYING:
            st["state"] = READY  # 买入信号未成交 → 回准备态
        hold = 0

    # 成本价变化（加仓/做T摊成本）→ 最高价基线重置
    cost = float(st.get("cost") or 0)
    if cost and float(st.get("highest_price") or 0) < cost:
        st["highest_price"] = round(cost, 4)
    if cur:
        st["highest_price"] = round(max(float(st.get("highest_price") or 0), float(cur)), 4)

    pnl_pct = round((cur - cost) / cost * 100, 2) if cost and cur else 0.0
    profitable = pnl_pct > 0
    # 盈利≥10% → 进入 PROTECT（盈利保护态，禁止追涨加仓）
    if pos_exist and pnl_pct >= 10 and st["state"] in (HOLD, READY, WATCH):
        st["state"] = PROTECT

    # ── 1) 退出触发分类（P0 强制退出 / P2 保护利润 / P1 风险降低） ──
    trigs = exit_triggers or []
    p0_hard = [t for t in trigs if t.get("kind") in ("止损退出", "趋势退出清仓", "时间退出")]
    p2_profit = [t for t in trigs if t.get("kind") == "盈利保护"]
    p1_trend = [t for t in trigs if t.get("kind") == "趋势退出减仓"]

    notes, reasons = [], []
    prev_action = st.get("last_action") or ACT_HOLD
    prev_time = st.get("last_action_time") or ""
    raw_up = raw_action == "buy"
    raw_down = raw_action == "sell"
    grade, score = quality or "", score or 0

    # 重大变化判定（反转门槛的硬证据）
    grade_drop = False
    try:
        _gmap = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1}
        if st.get("grade") and grade:
            if _gmap.get(grade, 3) <= _gmap.get(st["grade"], 3) - GRADE_DROP_MAJOR:
                grade_drop = True
    except Exception:
        pass
    score_drop = (float(st.get("score") or 0) - float(score)) >= SCORE_CHANGE_MAJOR

    # ── 2) 最终动作裁决（优先级 P0 > P1 > P2 > P3持有 > P4加仓 > P5买入） ──
    action = ACT_HOLD

    if p0_hard:
        # P0 强制退出：任何信号都压不住
        action = ACT_SELL
        reasons.append(f"{p0_hard[0]['kind']}触发（P0强制退出）")
        notes.append(f"🚨 P0强制退出：{p0_hard[0]['kind']} → 清仓，禁止因趋势/评分好而继续持有或加仓")
        st["state"] = EXIT
    elif p2_profit:
        # P2 保护利润：盈利≥10%后自峰值回撤8%
        action = ACT_SELL
        reasons.append("盈利保护触发（P2保护利润）")
        notes.append("🛡️ P2盈利保护：盈利≥10%后回撤8%，保护利润离场")
        st["state"] = EXIT
    elif p1_trend:
        # P1 风险降低：趋势退出减仓级（破MA10减半）
        action = ACT_REDUCE
        reasons.append("趋势退出减仓（P1风险降低）")
        notes.append("📉 P1风险降低：趋势转弱，按退出系统减仓")
        st["state"] = HOLD if pos_exist else WATCH
    elif market_state == "D":
        # 市场D级总闸门：只处理减仓/止损
        if raw_down:
            action = ACT_REDUCE
            reasons.append("市场D级+减仓信号（P1风险降低）")
            notes.append("🛑 P1风险降低：市场D级，只处理减仓/止损")
        else:
            notes.append("🛑 市场D级：禁止买入/加仓，维持持有，只处理止损")
    elif raw_down:
        # 减仓意图（C/D 级信号）——先过反转门槛（防"刚加仓就喊减"）
        if prev_action in (ACT_BUY, ACT_ADD) and prev_time and \
           _minutes_between(prev_time, _fmt(now)) < COOLDOWN_ADD * 2 and not (grade_drop or score_drop or grade == "D"):
            action = ACT_HOLD
            notes.append(f"🔄 反转门槛：上次{_ACTION_LABEL.get(prev_action)}后仅小幅回落（{st.get('grade','?')}→{grade}），趋势未破坏，维持持有；跌破MA20/硬止损再减")
            reasons.append("弱回落不反转（防抖动）")
        elif prev_action in (ACT_REDUCE, ACT_SELL) and prev_time and \
             _minutes_between(prev_time, _fmt(now)) < COOLDOWN_REDUCE_REPEAT and not p0_hard:
            action = ACT_HOLD
            notes.append("⏳ 减仓信号重复（60分钟内已提示过）：维持上次减仓建议，不重复喊单")
            reasons.append("减仓信号冷却期内重复")
        else:
            # 减仓分级（V3.0 第9节）：C=二级减仓 / D=三级（趋势破坏，破前低清仓）
            action = ACT_SELL if grade == "D" else ACT_REDUCE
            if grade == "D":
                notes.append("🔴 减仓三级：D级信号（趋势破坏）→ 清仓级，破前低必须走")
            else:
                notes.append("🟠 减仓二级：C级信号 → 减仓，破前低才清仓")
            reasons.append(f"信号降至{grade}级（减仓）")
    elif raw_up:
        # 向上意图（买入/加仓）—— 资格 + 冷却 + 反转门槛
        if not can_buy:
            action = ACT_HOLD
            notes.append("⚪ 趋势未确认/市场闸门关闭：买入/加仓降级为观望")
        elif not pos_exist:
            # P5 买入（建仓）：需 S/A 级
            if grade not in ("S", "A"):
                action = ACT_HOLD
                notes.append(f"⚪ 建仓信号强度不足（需S/A级，当前{grade}）：维持观察，等待信号转强")
            else:
                # 刚离场冷静期：120分钟内不回场
                if prev_action in (ACT_REDUCE, ACT_SELL) and prev_time and \
                   _minutes_between(prev_time, _fmt(now)) < COOLDOWN_REVERSE:
                    action = ACT_HOLD
                    notes.append(f"⏳ 刚{_ACTION_LABEL.get(prev_action)}（{COOLDOWN_REVERSE}分钟冷静期），不宜立刻回场，先观察企稳")
                    reasons.append("离场冷静期内不建仓")
                # 同向冷却：买入信号30分钟内重复 → 维持，不重复喊单
                elif prev_action == ACT_BUY and prev_time and \
                     _minutes_between(prev_time, _fmt(now)) < COOLDOWN_BUY:
                    action = ACT_HOLD
                    notes.append(f"⏳ 买入信号冷却期（{COOLDOWN_BUY}分钟）：维持上次建仓建议，不重复喊单")
                    reasons.append("买入信号冷却期内重复")
                else:
                    action = ACT_BUY
                    notes.append(f"🟢 P5买入：{grade}级信号成立，可建仓")
                    reasons.append(f"{grade}级建仓信号成立")
                    st["state"] = BUYING
        else:
            # P4 加仓（持仓内）：PROTECT禁加 / 亏损禁加 / 强度 / 冷却 / 反转门槛
            if st["state"] == PROTECT:
                action = ACT_HOLD
                notes.append("🛡️ PROTECT盈利保护态：禁止追涨加仓，只允许持有/减仓/卖出")
                reasons.append("盈利保护态禁止加仓")
            elif not profitable:
                action = ACT_HOLD
                notes.append(f"🚫 亏损持仓禁止加仓（V3.0纪律）：现价{cur} < 成本{cost}，只可做T摊成本，不可加仓")
                reasons.append("亏损持仓禁止加仓")
            elif grade not in ("S", "A", "B"):
                action = ACT_HOLD
                notes.append(f"⚪ 加仓信号强度不足（需S/A/B级，当前{grade}）：维持持有")
            elif prev_action in (ACT_BUY, ACT_ADD) and prev_time and \
                 _minutes_between(prev_time, _fmt(now)) < (COOLDOWN_ADD if prev_action == ACT_ADD else COOLDOWN_BUY):
                action = ACT_HOLD
                notes.append(f"⏳ 冷却期（{COOLDOWN_ADD if prev_action == ACT_ADD else COOLDOWN_BUY}分钟）内信号延续：维持上次{_ACTION_LABEL.get(prev_action)}建议，不重复喊单")
                reasons.append("冷却期内信号延续")
            elif prev_action in (ACT_REDUCE, ACT_SELL) and prev_time and \
                 _minutes_between(prev_time, _fmt(now)) < COOLDOWN_REVERSE:
                action = ACT_HOLD
                notes.append(f"⏳ 刚{_ACTION_LABEL.get(prev_action)}（{COOLDOWN_REVERSE}分钟冷静期），不宜立刻加回，先观察企稳")
                reasons.append("减仓后冷静期内不加回")
            else:
                action = ACT_ADD
                notes.append(f"🟢 P4加仓：{grade}级+盈利状态+趋势增强，可加仓")
                reasons.append(f"{grade}级盈利加仓信号成立")
    else:
        # 无动作意图（hold/watch）→ P3 持有
        action = ACT_HOLD
        if st["state"] == PROTECT:
            notes.append("🛡️ PROTECT盈利保护态：持有或分批止盈，禁止追涨加仓")
        else:
            notes.append("⚪ P3持有：信号未转强，维持现状")

    # 一级减仓提示（V3.0 第9节：RSI 过热只提示不动手，区分"提示/减仓/清仓"三级）
    if rsi is not None and rsi >= 80:
        notes.append(f"⚠️ 减仓一级提示：RSI{rsi}进入超买区(≥80)，不追高；冲高分批止盈或做T高抛")

    # ── 3) T+1 可卖份额（减仓/清仓限定可卖份额；当日买入不可卖） ──
    today_bought = _today_bought_shares(code) if pos_exist else 0
    if pos_exist and hold and today_bought:
        st["today_bought"] = today_bought
    can_sell = None
    if pos_exist and hold:
        tb = today_bought or int(st.get("today_bought") or 0)
        # 份额优先用持仓记录 quantity（事实），缺失才用金额/现价换算（round 防浮点截断）
        try:
            _q = pos.get("shares") or pos.get("quantity")
            if _q:
                total_shares = int(float(_q))
            else:
                _unit_price = float(cur) if cur else float(pos.get("buy_price") or 1)
                total_shares = int(round(float(hold) / _unit_price)) if _unit_price else 0
        except Exception:
            total_shares = 0
        can_sell = max(total_shares - tb, 0)
        if action in (ACT_REDUCE, ACT_SELL) and can_sell <= 0:
            action = ACT_HOLD
            notes.append("⚠️ T+1约束：今日买入份额不可卖，今日无法减仓，明日再处理")
            reasons.append("T+1当日买入不可卖")

    # ── 4) 变化检测 + 历史记录 ──
    changed = action != prev_action
    if changed and action != ACT_HOLD:
        append_history({
            "code": code, "name": name, "time": _fmt(now),
            "old_action": _ACTION_LABEL.get(prev_action, prev_action),
            "new_action": _ACTION_LABEL.get(action, action),
            "price": cur, "quality": grade, "score": score,
            "state": st["state"],
            "change_reason": reasons or ["信号变化"],
        })

    # ── 5) 状态迁移 + 持久化 ──
    if action == ACT_ADD:
        st["state"] = HOLD if st["state"] != PROTECT else PROTECT
    elif action == ACT_REDUCE:
        st["state"] = HOLD if pos_exist else WATCH
    elif action == ACT_SELL:
        st["state"] = EXIT if pos_exist else WATCH
    elif action == ACT_BUY:
        st["state"] = BUYING
    else:  # HOLD：非退出动作时 EXIT 态回 HOLD（决定继续持有；套牢票/人工不执行清仓的场景）
        if st["state"] == EXIT and pos_exist:
            st["state"] = HOLD
            # 决定不退出 → 上次动作同步更新为"持有"（否则残留"清仓"误导）
            st["last_action"] = ACT_HOLD
            st["last_action_time"] = _fmt(now)
    st["grade"] = grade
    st["score"] = float(score)
    if action != ACT_HOLD:
        st["last_action"] = action
        st["last_action_time"] = _fmt(now)
    states[code] = st  # 关键：新标的/更新后的状态必须写回（否则状态永不持久化）
    save_states(states)

    # ── 6) 输出文本（四段式：当前状态/变化/动作/说明） ──
    _prev_lbl = _ACTION_LABEL.get(prev_action, prev_action)
    _prev_t = f"({prev_time[11:16]})" if prev_time else ""
    _protect_tag = "（盈利保护）" if st["state"] == PROTECT else ""
    lines = [f"  📋 状态机: {st['state']}{_protect_tag} ｜ 上次动作: {_prev_lbl}{_prev_t} → 本次: {_ACTION_LABEL.get(action, action)}"]
    for _n in notes[:2]:  # 最多2条说明，防报告膨胀
        lines.append(f"  {_n}")
    if changed and reasons:
        lines.append(f"  🔄 变化原因: {'；'.join(reasons[:3])}")

    return {
        "state": st["state"],
        "prev_action": prev_action,
        "action": action,
        "changed": changed,
        "change_reason": reasons,
        "lines": lines,
        "can_sell_shares": can_sell,
        "today_bought": today_bought,
    }


# ─────────────────────────── 独立工具 ───────────────────────────

def print_state(code):
    """CLI 查看某标的当前状态"""
    st = load_states().get(code)
    if not st:
        print(f"{code}: 无状态记录")
        return
    for k, v in st.items():
        print(f"  {k}: {v}")


def print_all_states():
    for code, st in load_states().items():
        print(f"  {code} {st.get('name','')}: {st.get('state','')} 上次动作={st.get('last_action','')} {st.get('last_action_time','')}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--states":
        print_all_states()
    elif len(sys.argv) > 2 and sys.argv[1] == "--state":
        print_state(sys.argv[2])
    else:
        print(__doc__)
