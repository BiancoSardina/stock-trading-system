#!/usr/bin/env python3
"""
paper_trader.py — 虚拟交易跟踪（v2.31 补丁）

用途：不真实下单，仅按 signal_log.csv 的信号自动执行"虚拟买卖"，
     跟踪虚拟账户表现，验证策略规则是否真的有效（与真实持仓隔离）。

设计：
- 虚拟账户：初始资金 100,000 元（虚拟，与真实 10 万总资金分开算）
- 虚拟资金池：每笔买入用虚拟资金的 5%（≈5000元），单标的虚拟持仓上限 30%
- 信号执行规则（与真实交易纪律一致）：
    · 买入信号（等级 S/A/B）→ 虚拟买入（C/D 不买）
    · 卖出/减仓信号 → 虚拟卖出（有虚拟持仓才执行）
    · D级市场（mkt_state=D）→ 信号标记"已忽略"（禁买闸门），不虚拟买入
- 状态字段：每行信号的状态被更新为 虚拟买入/虚拟卖出/已忽略/待执行
  （更新写回 signal_log.csv，供复盘查看信号是否被虚拟执行）
- 虚拟持仓存 paper_positions.json（含策略版本）

用法：
  python3 paper_trader.py           # 回放全部信号，输出虚拟账户报告
  python3 paper_trader.py --recent  # 只看最近30个信号后的虚拟表现
  python3 paper_trader.py --reset   # 清空虚拟账户重新回放

由 review.py 集成（每日 18:15 复盘一并输出虚拟账户）。
"""
import csv
import json
import os
import sys
import urllib.request
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNAL_LOG = os.path.join(SCRIPT_DIR, "signal_log.csv")
PAPER_FILE = os.path.join(SCRIPT_DIR, "paper_positions.json")

VIRTUAL_CAPITAL = 200000   # 虚拟初始资金（2026-08-12 用户要求 10万→20万）
BUY_RATIO = 0.05           # 每笔虚拟买入 = 虚拟资金的5%
MAX_POS_RATIO = 0.30       # 单标的上限 30%
STATE_PENDING = "待执行"
STATE_BOUGHT = "虚拟买入"
STATE_SOLD = "虚拟卖出"
STATE_IGNORED = "已忽略"


# ─────────────────────────── 行情工具 ───────────────────────────
def sina_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://finance.sina.com.cn"})
    return urllib.request.urlopen(req, timeout=10).read()


def get_prefix(code):
    return "sh" if code.startswith(("5", "6", "9")) else "sz"


def get_rt(code):
    try:
        pref = get_prefix(code)
        data = sina_get(f"https://hq.sinajs.cn/list={pref}{code}").decode("gbk")
        parts = data.split(",")
        if len(parts) >= 4:
            return {"cur": float(parts[3]), "prev": float(parts[2]),
                    "high": float(parts[4]), "low": float(parts[5])}
    except Exception:
        pass
    return None


# ─────────────────────────── 虚拟账户持久化 ───────────────────────────
def load_paper():
    """读取虚拟账户：{cash, positions:{code:{...}}, trades:[...], last_index}"""
    if os.path.isfile(PAPER_FILE):
        try:
            with open(PAPER_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"cash": VIRTUAL_CAPITAL, "positions": {}, "trades": [], "last_index": 0}


def save_paper(paper):
    with open(PAPER_FILE, "w", encoding="utf-8") as f:
        json.dump(paper, f, ensure_ascii=False, indent=2)


def reset_paper():
    paper = {"cash": VIRTUAL_CAPITAL, "positions": {}, "trades": [], "last_index": 0}
    save_paper(paper)
    return paper


# ─────────────────────────── 虚拟交易执行 ───────────────────────────
def paper_buy(paper, sig, price):
    """虚拟买入：金额 = 当前虚拟总资产5%（动态），单标的≤30%虚拟资产，现金不足则跳过"""
    code = sig["code"]
    total_value = paper["cash"] + sum(p["shares"] * p["price"] for p in paper["positions"].values())
    amount = total_value * BUY_RATIO
    # 单标的上限：虚拟总资产30%
    max_pos = total_value * MAX_POS_RATIO
    cur_pos = paper["positions"].get(code, {}).get("shares", 0) * price
    if cur_pos + amount > max_pos:
        amount = max(0, max_pos - cur_pos)
    if amount < 1000:
        return False  # 额度不足或已超上限，跳过
    # 现金不足 → 按可用现金缩量（不足1手跳过）
    if amount > paper["cash"]:
        amount = paper["cash"]
    shares = int(amount / price / 100) * 100
    if shares <= 0:
        return False
    cost = shares * price
    if cost > paper["cash"]:
        shares = int(paper["cash"] / price / 100) * 100
        if shares <= 0:
            return False
        cost = shares * price
    paper["cash"] -= cost
    pos = paper["positions"].setdefault(code, {
        "name": sig["name"], "shares": 0, "price": 0.0,
        "buy_date": sig["date"], "grade": sig["grade"],
        "version": sig.get("version", "?"),
    })
    # 摊薄成本
    old_cost = pos["shares"] * pos["price"]
    pos["shares"] += shares
    pos["price"] = round((old_cost + cost) / pos["shares"], 4) if pos["shares"] else 0
    pos["buy_date"] = pos.get("buy_date") or sig["date"]
    pos["grade"] = sig["grade"] if pos["grade"] in ("", "?") else pos["grade"]
    pos["version"] = sig.get("version", pos.get("version", "?"))
    paper["trades"].append({
        "date": sig["date"], "code": code, "name": sig["name"],
        "action": "虚拟买入", "price": price, "shares": shares,
        "amount": round(cost, 2), "grade": sig["grade"],
        "version": sig.get("version", "?"),
    })
    return True


def paper_sell(paper, sig, price):
    """虚拟卖出：有虚拟持仓才执行，全部卖出"""
    code = sig["code"]
    pos = paper["positions"].get(code)
    if not pos or pos["shares"] <= 0:
        return False
    shares = pos["shares"]
    proceeds = shares * price
    paper["cash"] += proceeds
    paper["trades"].append({
        "date": sig["date"], "code": code, "name": pos["name"],
        "action": "虚拟卖出", "price": price, "shares": shares,
        "amount": round(proceeds, 2), "grade": sig["grade"],
        "version": sig.get("version", pos.get("version", "?")),
        "pnl": round((price - pos["price"]) * shares, 2),
        "pnl_pct": round((price / pos["price"] - 1) * 100, 2) if pos["price"] else 0,
    })
    paper["positions"].pop(code, None)
    return True


# ─────────────────────────── 信号回放 ───────────────────────────
def load_signals():
    if not os.path.isfile(SIGNAL_LOG):
        return []
    rows = []
    with open(SIGNAL_LOG, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append({
                "date": r.get("时间", ""), "code": r.get("代码", ""),
                "name": r.get("名称", ""), "action": r.get("操作", ""),
                "price": float(r.get("价格", 0) or 0),
                "grade": r.get("信号等级", ""),
                "score": r.get("评分", ""),
                "mkt_state": r.get("市场状态", ""),
                "status": r.get("状态", STATE_PENDING),
                "version": r.get("策略版本", ""),
            })
    return rows


def update_signal_status(code, date_str, action, new_status):
    """把 signal_log.csv 中匹配行的状态字段更新（写回）"""
    if not os.path.isfile(SIGNAL_LOG):
        return
    try:
        with open(SIGNAL_LOG, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        if not lines:
            return
        header = lines[0].split(",")
        # 找状态列与匹配列索引
        try:
            idx_status = header.index("状态")
        except ValueError:
            return  # 旧表头无状态列，跳过
        try:
            idx_code = header.index("代码")
            idx_date = header.index("时间")
            idx_action = header.index("操作")
        except ValueError:
            return
        changed = False
        for i in range(1, len(lines)):
            parts = lines[i].split(",")
            if len(parts) <= idx_status:
                continue
            if (parts[idx_code] == code and parts[idx_date].startswith(date_str[:10])
                    and parts[idx_action] == action and parts[idx_status] == STATE_PENDING):
                parts[idx_status] = new_status
                lines[i] = ",".join(parts)
                changed = True
        if changed:
            with open(SIGNAL_LOG, "w", encoding="utf-8", newline="") as f:
                f.write("\n".join(lines))
    except Exception:
        pass


def replay(paper):
    """回放 signal_log 全部信号到虚拟账户（从 last_index 增量，按代码+日期+方向去重）"""
    signals = load_signals()
    start = paper.get("last_index", 0)
    processed = 0
    seen = set(tuple(k) for k in paper.get("seen_keys", []))  # 已处理的 (code, date, action) 去重键
    seen_keys_out = set(seen)  # 保持 tuple 集合，序列化时转 list
    for i in range(start, len(signals)):
        sig = signals[i]
        if not sig["code"] or not sig["price"]:
            continue
        key = (sig["code"], sig["date"][:10], sig["action"])
        if key in seen:
            continue  # 同日同标的同方向的重复信号（多时段运行产生）只处理首次
        seen.add(key)
        # D级市场闸门：买入信号忽略（禁买），卖出仍执行
        if sig["action"] in ("买入", "加仓", "买入/加仓"):
            if sig.get("mkt_state") == "D":
                update_signal_status(sig["code"], sig["date"], sig["action"], STATE_IGNORED)
            else:
                ok = paper_buy(paper, sig, sig["price"])
                if ok:
                    update_signal_status(sig["code"], sig["date"], sig["action"], STATE_BOUGHT)
        elif sig["action"] in ("卖出", "减仓", "卖出/减仓"):
            ok = paper_sell(paper, sig, sig["price"])
            if ok:
                update_signal_status(sig["code"], sig["date"], sig["action"], STATE_SOLD)
        processed += 1
        seen_keys_out.add(key)
    paper["last_index"] = len(signals)
    paper["seen_keys"] = [list(k) for k in seen_keys_out]
    save_paper(paper)
    return processed


# ─────────────────────────── 虚拟账户报告 ───────────────────────────
def generate_report(paper, recent_only=False):
    lines = []
    positions = paper.get("positions", {})
    trades = paper.get("trades", [])
    cash = paper.get("cash", VIRTUAL_CAPITAL)

    # 未平仓持仓的浮动盈亏（用实时价）
    float_pnl = 0.0
    pos_lines = []
    for code, p in positions.items():
        rt = get_rt(code)
        cur = rt["cur"] if rt else p["price"]
        mv = p["shares"] * cur
        pnl = mv - p["shares"] * p["price"]
        pnl_pct = (cur / p["price"] - 1) * 100 if p["price"] else 0
        float_pnl += pnl
        pos_lines.append((p, cur, mv, pnl, pnl_pct))

    # 已实现盈亏
    realized = sum(t.get("pnl", 0) for t in trades if t["action"] == "虚拟卖出")
    total_value = cash + sum(p["shares"] * c for p, c, *_ in pos_lines)
    total_pnl = total_value - VIRTUAL_CAPITAL
    total_pnl_pct = (total_value / VIRTUAL_CAPITAL - 1) * 100

    lines.append("\n🖥️ 【虚拟交易跟踪】 (独立虚拟账户10万，仅回放信号不真实下单)")
    lines.append(f"  虚拟总资产: {total_value:,.0f}元 (现金{cash:,.0f} + 持仓{total_value-cash:,.0f})")
    lines.append(f"  总盈亏: {total_pnl:+,.0f}元 ({total_pnl_pct:+.2f}%) | 已实现: {realized:+,.0f}元 | 浮动: {float_pnl:+,.0f}元")
    if pos_lines:
        lines.append(f"  {'标的':<10} {'虚拟持仓':>8} {'成本':>7} {'现价':>7} {'市值':>9} {'浮动盈亏':>9}")
        lines.append("  " + "-" * 52)
        for p, cur, mv, pnl, pnl_pct in pos_lines:
            icon = "🟢" if pnl >= 0 else "🔴"
            lines.append(f"  {p['name'][:6]:<6} {p['shares']:>6}份 {p['price']:>7.3f} {cur:>7.3f} "
                         f"{mv:>9,.0f} {icon}{pnl:+,.0f}元({pnl_pct:+.1f}%)")
    else:
        lines.append("  当前无虚拟持仓（全部已卖出或未买入）")

    # 虚拟交易胜率（已卖出 + 未平仓按现价虚拟平仓，两个口径）
    sells = [t for t in trades if t["action"] == "虚拟卖出" and t.get("pnl") is not None]
    # 未平仓按现价虚拟平仓（口径2：假设现在全部清仓）
    paper_closes = []
    for code, p in positions.items():
        rt = get_rt(code)
        cur = rt["cur"] if rt else p["price"]
        pnl_pct = (cur / p["price"] - 1) * 100 if p["price"] else 0
        paper_closes.append({"grade": p.get("grade", ""), "pnl": (cur - p["price"]) * p["shares"],
                             "pnl_pct": pnl_pct, "code": code, "name": p.get("name", "")})

    def _stats(ts, title):
        if not ts:
            return f"  {title}: 暂无数据"
        from collections import defaultdict
        g = defaultdict(list)
        for t in ts:
            g[t["grade"]].append(t)
        lines = [f"  📊 【{title}】"]
        lines.append(f"  {'等级':<6} {'笔数':>5} {'胜率':>8} {'平均收益':>9}")
        lines.append("  " + "-" * 34)
        for gr in ["S", "A", "B", "C", "D"]:
            gl = g.get(gr, [])
            if not gl:
                continue
            wins = [t for t in gl if t["pnl"] > 0]
            wr = len(wins) / len(gl) * 100
            avg = sum(t["pnl_pct"] for t in gl) / len(gl)
            lines.append(f"  {gr}级     {len(gl):>4}笔  {wr:>6.1f}%  {avg:>+8.2f}%")
        tw = len([t for t in ts if t["pnl"] > 0])
        lines.append(f"  总胜率: {tw}/{len(ts)} = {tw/len(ts)*100:.1f}%")
        return "\n".join(lines)

    if sells:
        print_extra = _stats(sells, "虚拟胜率·已平仓")
    else:
        print_extra = "  📊 【虚拟胜率·已平仓】暂无（尚无卖出信号，积累中）"
    if paper_closes:
        print_extra += "\n" + _stats(paper_closes, "虚拟胜率·按现价平仓")
    lines.append(print_extra)
    return "\n".join(lines)


def main():
    import qq_send
    out = []
    if "--reset" in sys.argv:
        paper = reset_paper()
        out.append("✅ 虚拟账户已重置，重新回放...")
    else:
        paper = load_paper()

    processed = replay(paper)
    out.append(f"🖥️ 虚拟交易回放: 处理 {processed} 条信号 (累计 {len(paper['trades'])} 笔虚拟交易)")
    out.append(generate_report(paper, recent_only="--recent" in sys.argv))

    # 分段直发到 QQ（QQ 单条消息限长，必须分段）；失败则输出原文由 cron 兜底
    report = "\n".join(out)
    if not qq_send.push_or_stdout(report):
        print(report)


if __name__ == "__main__":
    main()
