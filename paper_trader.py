#!/usr/bin/env python3
"""
paper_trader.py — 虚拟交易跟踪（v2.31 补丁）

用途：不真实下单，仅按 signal_log.csv 的信号自动执行"虚拟买卖"，
     检查规则回放的记账结果，不用于证明策略有效（与真实持仓隔离）。

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
from runtime import data_path, read_json, atomic_json, exclusive
from signal_store import read_signals
from paper_execution import buy as paper_buy, sell as paper_sell

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNAL_LOG = data_path("signal_log_v32.csv")
PAPER_FILE = data_path("paper_positions_v32.json")

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
def fresh_paper():
    return {"schema": "v3.1", "cash": VIRTUAL_CAPITAL, "positions": {}, "trades": [],
            "receipts": {}, "last_time": "", "fill_model": "报价±可配置滑点；不保证成交"}


def load_paper():
    paper = read_json(PAPER_FILE, None)
    if paper is None:
        return fresh_paper()
    if not isinstance(paper, dict) or paper.get("schema") != "v3.1":
        raise ValueError("模拟账户版本不符，禁止覆盖历史账户")
    return paper


def save_paper(paper):
    atomic_json(PAPER_FILE, paper)


def reset_paper():
    # Explicit --reset affects only the versioned hypothetical account.
    with __import__("runtime").file_lock(PAPER_FILE):
        paper = fresh_paper()
        save_paper(paper)
        return paper


def load_signals():
    return read_signals(SIGNAL_LOG)


@exclusive(lambda: PAPER_FILE)
def replay(paper):
    from copy import deepcopy
    # Refresh inside the lock to avoid overwriting another run's fills.
    work = load_paper() if os.path.exists(PAPER_FILE) else deepcopy(paper)
    if work.get("schema") != "v3.1":
        raise ValueError("旧账户不可用于新规则回放")
    processed = 0
    for sig in sorted(load_signals(), key=lambda row: (row["date"], row["id"])):
        sid = sig["id"]
        if sid in work["receipts"]:
            continue
        status = "已忽略"
        if sig["date"] < work["last_time"]:
            status = "迟到历史信号，需独立重新回放"
        else:
            try:
                quote_time = datetime.strptime(sig.get("quote_time", ""), "%Y-%m-%d %H:%M:%S")
                signal_time = datetime.strptime(sig["date"], "%Y-%m-%d %H:%M")
                # Minute-resolution signal timestamps tolerate the quote's seconds.
                fresh = -60 < (signal_time - quote_time).total_seconds() <= 300
            except ValueError:
                fresh = False
            if fresh:
                if sig["new_action"] in ("买入", "加仓"):
                    if paper_buy(work, sig, sig["price"]):
                        status = "虚拟买入"
                elif paper_sell(work, sig, sig["price"]):
                    status = "虚拟卖出"
            else:
                status = "行情时间无效，未模拟成交"
            work["last_time"] = max(work["last_time"], sig["date"])
        work["receipts"][sid] = status
        processed += 1
    save_paper(work)  # Fills, cash, lots and receipts commit together.
    paper.clear()
    paper.update(work)
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

    lines.append(f"\n🖥️ 【规则模拟 v3.2】初始{VIRTUAL_CAPITAL:,.0f}元；报价±滑点假设成交，含设定费用，不代表真实成交")
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
