#!/usr/bin/env python3
"""
review.py — 交易复盘系统（第八阶段）

用途：读取 signal_log.csv（信号日志）+ positions.json（持仓），
     统计v3.1信号后五个完整交易日的毛收益，不代表实际成交收益，
     回答"哪些规则真的有效"。

统计口径：
- 买卖配对：同一代码按时间顺序，买入信号 → 后续卖出/减仓信号 配对为一笔交易；
  若买入后至今未卖出，用当前价作为未实现结果（标记"未平仓"）。
- 每笔交易字段：买入时间、买入价格、评分、行业、市场评分、卖出价格、
  最大盈利（买入后至卖出前最高价-买入价）、最大亏损（买入后至卖出前最低价-买入价）、
  持有天数、结果（盈利/亏损）。
- 按信号等级聚合：笔数、胜率、平均收益、平均最大盈利/亏损。

用法：python3 review.py [--recent N]   # N=只统计最近N笔（默认全部）
输出：复盘报告（stdout），由 cron 任务投递。
"""
import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, date
from runtime import data_path, positive
from signal_store import read_signals

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNAL_LOG = data_path("signal_log_v32.csv")
POSITIONS = data_path("positions.json")


# ─────────────────────────── 行情工具 ───────────────────────────
def sina_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://finance.sina.com.cn"})
    return urllib.request.urlopen(req, timeout=10).read()


def get_prefix(code):
    return "sh" if code.startswith(("5", "6", "9")) else "sz"


def get_rt(code):
    """实时价"""
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


def get_kline(code, days=120):
    """日K线：[{day,high,low,close,...},...]（quotes.sina.cn 抗限流端点；money.finance 连续请求会触发 HTTP 456）"""
    try:
        pref = get_prefix(code)
        url = (f"https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
               f"?symbol={pref}{code}&scale=240&ma=5&datalen={days}")
        return json.loads(sina_get(url).decode("utf-8", "ignore"))
    except Exception:
        return []


def trading_days_between(d1, d2):
    """两个日期之间的交易日数（近似：周一~周五）"""
    try:
        a = datetime.strptime(d1[:10], "%Y-%m-%d").date()
        b = datetime.strptime(d2[:10], "%Y-%m-%d").date()
        if b < a:
            a, b = b, a
        days = 0
        d = a
        while d < b:
            if d.weekday() < 5:
                days += 1
            d = date.fromordinal(d.toordinal() + 1)
        return days
    except Exception:
        return 0


# ─────────────────────────── 数据加载 ───────────────────────────
def load_signals():
    return read_signals(SIGNAL_LOG)


def load_positions():
    """读取 positions.json 获取未平仓持仓（过滤已平仓 status=sold/closed）"""
    if not os.path.isfile(POSITIONS):
        return {}
    try:
        with open(POSITIONS, "r", encoding="utf-8") as f:
            data = json.load(f)
        result = {}
        for grp in ("etf", "stock"):
            for p in data.get(grp, []):
                if p.get("status") in ("sold", "closed"):
                    continue
                result[p["code"]] = p
        return result
    except Exception:
        return {}


# ─────────────────────────── 交易配对 ───────────────────────────
def dedup_signals(signals):
    """
    去重：同一代码+同一日期+同一操作方向 只保留第一条（信号首次发出时刻）。
    signal_log.csv 是 append 模式，同一时段多次运行会产生重复信号行。
    """
    seen = set()
    result = []
    for s in sorted(signals, key=lambda x: x["date"]):
        # 去重键包含策略版本（不同版本的同名信号算不同策略）
        key = (s["code"], s["date"][:10], s["action"], s.get("version", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(s)
    return result


def pair_trades(signals, positions):
    """
    信号有效性统计（口径：信号发出后表现，而非真实成交配对）
    对每条买入信号：取信号日之后（不含当日）的K线，
    - 持有N日结果 = 信号后第5根完整K线收盘价 vs 信号价（不足5根不计）
    - 最大盈利 = 信号后五日最高价 vs 信号价
    - 最大亏损 = 信号后五日最低价 vs 信号价
    - 结果 = 持有N日收益（>0 计为"信号有效"）
    当日刚发出的信号（尚无后续K线）不计入胜率统计（result=None）。
    """
    signals = dedup_signals(signals)
    trades = []
    today = datetime.now().strftime("%Y-%m-%d")
    for s in signals:
        if s["action"] not in ("买入", "加仓", "买入/加仓"):
            continue
        trade = {
            "code": s["code"], "name": s["name"],
            "buy_time": s["date"], "buy_price": s["price"],
            "score": s["score"], "grade": s["grade"],
            "industry": s["industry"], "mkt_score": s["mkt_score"],
            "mkt_state": s["mkt_state"],
            "version": s.get("version", ""),
            "sell_time": "", "sell_price": None,
            "max_profit_pct": None, "max_loss_pct": None,
            "hold_days": None, "result": None, "closed": False,
        }
        buy_day = s["date"][:10]
        kline = get_kline(s["code"])
        # Only completed daily bars, and the same five-session window for all metrics.
        by_date = {}
        now = datetime.now()
        for k in kline or []:
            day = str(k.get("day", ""))[:10]
            if day <= buy_day or day > today or (day == today and now.hour < 15):
                continue
            if all(positive(k.get(field)) for field in ("close", "high", "low")):
                by_date[day] = k
        future = [by_date[day] for day in sorted(by_date)][:5]
        if len(future) < 5:
            trade["hold_days"] = len(future)
            trade["pending_reason"] = "不足五个完整交易日或历史行情缺失"
            trades.append(trade)
            continue
        n = 5
        exit_k = future[n - 1]
        trade["sell_time"] = str(exit_k.get("day", ""))[:10]
        trade["sell_price"] = float(exit_k["close"])
        trade["hold_days"] = n
        trade["result"] = round((float(exit_k["close"]) / s["price"] - 1) * 100, 2) if s["price"] else None
        peak = max(float(k["high"]) for k in future)
        trough = min(float(k["low"]) for k in future)
        trade["max_profit_pct"] = round((peak / s["price"] - 1) * 100, 2) if s["price"] else None
        trade["max_loss_pct"] = round((trough / s["price"] - 1) * 100, 2) if s["price"] else None
        trade["closed"] = True
        trades.append(trade)
    return trades


# ─────────────────────────── 统计 ───────────────────────────
def stats_by_grade(trades):
    """按信号等级聚合：笔数、胜率、平均收益"""
    from collections import defaultdict
    g = defaultdict(list)
    for t in trades:
        if t["result"] is not None:
            g[t["grade"]].append(t)

    lines = ["\n📊 【信号等级胜率统计】"]
    lines.append(f"  {'等级':<6} {'笔数':>5} {'胜率':>8} {'平均收益':>9} {'平均最大盈利':>10} {'平均最大亏损':>10}")
    lines.append("  " + "-" * 58)
    grade_order = ["S", "A", "B", "C", "D"]
    for gr in grade_order:
        ts = g.get(gr, [])
        if not ts:
            continue
        wins = [t for t in ts if t["result"] > 0]
        win_rate = len(wins) / len(ts) * 100
        avg_pnl = sum(t["result"] for t in ts) / len(ts)
        avg_profit = sum(t["max_profit_pct"] or 0 for t in ts) / len(ts)
        avg_loss = sum(t["max_loss_pct"] or 0 for t in ts) / len(ts)
        icon = "✅" if win_rate >= 60 else "🟡" if win_rate >= 45 else "🔴"
        lines.append(f"  {gr}级     {len(ts):>4}笔  {icon}{win_rate:>6.1f}%  {avg_pnl:>+8.2f}%  {avg_profit:>+9.2f}%  {avg_loss:>+9.2f}%")
    return "\n".join(lines)


def stats_by_industry(trades):
    """按行业聚合（可选辅助维度）"""
    from collections import defaultdict
    g = defaultdict(list)
    for t in trades:
        if t["result"] is not None and t["industry"]:
            g[t["industry"]].append(t)
    if not g:
        return "\n📊 【行业胜率统计】暂无数据（行业字段自08-05开始记录，积累后自动出现）"
    lines = ["\n📊 【行业胜率统计】"]
    lines.append(f"  {'行业':<8} {'笔数':>5} {'胜率':>8} {'平均收益':>9}")
    lines.append("  " + "-" * 36)
    for ind, ts in sorted(g.items(), key=lambda x: -len(x[1])):
        wins = [t for t in ts if t["result"] > 0]
        win_rate = len(wins) / len(ts) * 100
        avg_pnl = sum(t["result"] for t in ts) / len(ts)
        lines.append(f"  {ind:<6} {len(ts):>4}笔  {win_rate:>6.1f}%  {avg_pnl:>+8.2f}%")
    return "\n".join(lines)


def stats_by_mktstate(trades):
    """按市场状态聚合：市场A/B/C/D级下买入的胜率差异（验证市场闸门有效性）"""
    from collections import defaultdict
    g = defaultdict(list)
    for t in trades:
        if t["result"] is not None and t["mkt_state"]:
            g[t["mkt_state"]].append(t)
    if not g:
        return "\n📊 【市场状态×胜率】暂无数据（市场状态字段自08-05开始记录，积累后自动出现）"
    lines = ["\n📊 【市场状态×胜率】（验证市场闸门：D级禁买是否有效）"]
    lines.append(f"  {'市场':<6} {'笔数':>5} {'胜率':>8} {'平均收益':>9}")
    lines.append("  " + "-" * 36)
    for st in ["A", "B", "C", "D"]:
        ts = g.get(st, [])
        if not ts:
            continue
        wins = [t for t in ts if t["result"] > 0]
        win_rate = len(wins) / len(ts) * 100
        avg_pnl = sum(t["result"] for t in ts) / len(ts)
        lines.append(f"  {st}级     {len(ts):>4}笔  {win_rate:>6.1f}%  {avg_pnl:>+8.2f}%")
    return "\n".join(lines)


def stats_by_version(trades):
    """按策略版本聚合（v2.31：对比不同策略版本的有效性）"""
    from collections import defaultdict
    g = defaultdict(list)
    for t in trades:
        if t["result"] is not None and t.get("version"):
            g[t["version"]].append(t)
    if not g:
        return "\n📊 【策略版本×胜率】暂无数据（策略版本字段自v2.31开始记录，积累后自动出现）"
    lines = ["\n📊 【策略版本×胜率】（对比各版本策略有效性）"]
    lines.append(f"  {'版本':<10} {'笔数':>5} {'胜率':>8} {'平均收益':>9}")
    lines.append("  " + "-" * 38)
    for ver, ts in sorted(g.items(), key=lambda x: -len(x[1])):
        wins = [t for t in ts if t["result"] > 0]
        win_rate = len(wins) / len(ts) * 100
        avg_pnl = sum(t["result"] for t in ts) / len(ts)
        lines.append(f"  {ver:<8} {len(ts):>4}笔  {win_rate:>6.1f}%  {avg_pnl:>+8.2f}%")
    return "\n".join(lines)


def stats_by_pool():
    """
    股票池表现统计（V1.1 接入）：读 stock_pool.json，对池内每只标的算
    「入池以来表现」（基准=first_seen 日收盘，取不到用生成时 price）+
    淘汰条件命中预警（破MA60/总分<70/行业<60）+ 按行业聚合平均涨跌幅。

    口径说明：当天刚生成的池（first_seen==今天）表现≈0，属正常；
    days_in_pool≥2 的旧票才体现真实选股质量，数据随天数积累。
    """
    pool_path = os.path.join(SCRIPT_DIR, "stock_pool.json")
    if not os.path.isfile(pool_path):
        return "\n📊 【股票池表现】暂无 stock_pool.json（17:30选股任务尚未运行）"
    try:
        with open(pool_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return f"\n📊 【股票池表现】读取失败: {e}"
    date = str(data.get("date", ""))[:10]
    core = data.get("core_pool", []) or []
    watch = data.get("watch_pool", []) or []
    if not core and not watch:
        return f"\n📊 【股票池表现】{date} 池为空"
    from collections import defaultdict
    lines = [f"\n📊 【股票池表现】({date} 生成, 市场{data.get('market_status','')}级{data.get('market_score','')}分)"]
    lines.append(f"  {'名称':<8} {'基准价':>7} {'现价':>7} {'入池以来':>8} {'20日':>7} {'距MA20':>7} {'状态'}")
    lines.append("  " + "-" * 62)
    core_codes = {e.get("code") for e in core}
    ind_g = defaultdict(list)
    for it in core + watch:
        code, name = it.get("code", ""), it.get("name", "")
        if not code:
            continue
        entry_price = it.get("price")
        ind = it.get("industry", "")
        kline = get_kline(code, 130)
        closes = [float(k["close"]) for k in kline]
        if not closes:
            continue
        cur = closes[-1]
        rt = get_rt(code)
        cur_rt = rt["cur"] if rt else cur
        # 基准价：first_seen 日收盘（K线内查找）→ 取不到用生成时 price
        base = entry_price
        fs = str(it.get("first_seen", ""))[:10]
        if fs:
            for k in kline:
                if str(k.get("day", ""))[:10] == fs:
                    base = float(k["close"])
                    break
        chg = (cur_rt / base - 1) * 100 if base else 0.0
        chg20 = (cur / closes[-21] - 1) * 100 if len(closes) >= 21 else 0.0
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
        ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
        dist_ma20 = (cur / ma20 - 1) * 100 if ma20 else 0.0
        # 淘汰条件命中预警（与 stock_pool_manager.evict_check 同口径）
        warns = []
        if ma60 and cur < ma60:
            warns.append("破MA60")
        if it.get("total_score") is not None and float(it.get("total_score", 0) or 0) < 70:
            warns.append("总分<70")
        if float(it.get("industry_score", 0) or 0) < 60:
            warns.append("行业<60")
        tag = "core" if code in core_codes else "watch"
        status = ("🔴 " + "/".join(warns)) if warns else (f"🟢 {tag}")
        lines.append(f"  {name[:6]:<6} {base:>7.2f} {cur_rt:>7.2f} {chg:>+7.2f}% {chg20:>+6.1f}% {dist_ma20:>+6.1f}% {status}")
        if ind:
            ind_g[ind].append(chg)
    if ind_g:
        lines.append("\n  【按行业聚合】")
        for ind, chgs in sorted(ind_g.items(), key=lambda x: -sum(x[1]) / len(x[1])):
            avg = sum(chgs) / len(chgs)
            lines.append(f"    {ind:<8} {len(chgs)}只 平均{avg:+.2f}%")
    lines.append("\n  💡 入池以来涨幅为正且未触发淘汰 = 选股有效；当天新票涨幅≈0属正常")
    return "\n".join(lines)


def detail_table(trades, recent=20):
    """最近N笔交易明细"""
    lines = [f"\n📋 【最近{min(recent, len(trades))}笔交易明细】"]
    lines.append(f"  {'名称':<8} {'等级':<4} {'买入时间':<16} {'买入价':>7} {'卖出价':>7} "
                 f"{'持有':>4} {'最大盈':>7} {'最大亏':>7} {'结果':>7}")
    lines.append("  " + "-" * 78)
    for t in trades[-recent:]:
        icon = "🟢" if (t["result"] or 0) > 0 else "🔴" if (t["result"] or 0) < 0 else "⚪"
        result_str = f"{icon}{t['result']:+.2f}%" if t["result"] is not None else "—"
        sell_str = f"{t['sell_price']:.3f}" if t["sell_price"] else "—"
        hold_str = f"{t['hold_days']}日" if t["hold_days"] is not None else "—"
        lines.append(f"  {t['name'][:6]:<6} {t['grade']:<4} {t['buy_time'][:16]:<16} {t['buy_price']:>7.3f} "
                     f"{sell_str:>7} {hold_str:>5} {t['max_profit_pct'] or 0:>+6.1f}% {t['max_loss_pct'] or 0:>+6.1f}% "
                     f"{result_str:>8}")
    return "\n".join(lines)


# ─────────────────────────── 主流程 ───────────────────────────
def main():
    import qq_send
    recent = None
    if len(sys.argv) > 1 and sys.argv[1] == "--recent":
        try:
            recent = int(sys.argv[2])
        except Exception:
            recent = None

    out = []
    def p(s=""):
        out.append(s)

    signals = load_signals()
    if not signals:
        p("⚠️ signal_log.csv 为空或无记录，暂无复盘数据。")
        report = "\n".join(out)
        if not qq_send.push_or_stdout(report):
            print(report)
        return
    positions = load_positions()
    trades = pair_trades(signals, positions)
    if not trades:
        p("⚠️ 未能配对出任何交易记录（需要至少一个买入信号）。")
        report = "\n".join(out)
        if not qq_send.push_or_stdout(report):
            print(report)
        return

    p("╔══════════════════════════════════════╗")
    p("║  📊 交易复盘系统（第八阶段）          ║")
    p(f"║  {datetime.now().strftime('%Y-%m-%d %H:%M')}                  ║")
    p("╚══════════════════════════════════════╝")
    p(f"\n📥 信号总数: {len(signals)}条 | 配对交易: {len(trades)}笔 "
      f"({sum(1 for t in trades if t['closed'])}笔五日观察完成)")

    # 全部交易统计 + 最近N笔统计
    if recent:
        p(f"\n📈 ==== 最近 {recent} 笔统计 ====")
        p(stats_by_grade(trades[-recent:]))
        p(stats_by_mktstate(trades[-recent:]))
        p("\n📈 ==== 全部历史统计 ====")
    p(stats_by_grade(trades))
    p(stats_by_mktstate(trades))
    p(stats_by_version(trades))
    p(stats_by_industry(trades))
    p(stats_by_pool())
    p(detail_table(trades, recent=20))

    # ETF T策略复盘（etf_t_engine 的 t_trade_log.csv：成功率/平均收益/适配度）
    try:
        import etf_t_engine
        p(etf_t_engine.review_stats())
    except Exception:
        pass

    # v2.31：虚拟交易跟踪已移至独立 18:20 任务（paper_trader.py）——避免与 review 报告重复输出；
    # 18:15 复盘不再内嵌虚拟账户报告，虚拟账户状态由 18:20 任务增量回放维护。
    # （2026-08-07 用户要求：去掉重复输出，只保留 18:20 独立报告）

    # 总结：哪些规则有效
    p("\n💡 【复盘结论】")
    from collections import defaultdict
    g = defaultdict(list)
    for t in trades:
        if t["result"] is not None:
            g[t["grade"]].append(t)
    best = None
    for gr in ["S", "A", "B", "C", "D"]:
        ts = g.get(gr, [])
        if len(ts) >= 3:
            wr = len([t for t in ts if t["result"] > 0]) / len(ts) * 100
            if best is None or wr > best[1]:
                best = (gr, wr, len(ts))
    if best:
        p(f"  🏆 已观察样本中命中率最高: {best[0]}级 {best[1]:.0f}% ({best[2]}笔) ；仅描述样本，不能据此提高仓位或认定策略有效")
    for gr in ["S", "A", "B", "C", "D"]:
        ts = g.get(gr, [])
        if len(ts) >= 3:
            wr = len([t for t in ts if t["result"] > 0]) / len(ts) * 100
            if wr < 40:
                p(f"  ⚠️ {gr}级胜率仅{wr:.0f}% ({len(ts)}笔) → 该等级信号需要收紧或降级处理")
    p("\n⚠️ 复盘为信号统计参考，不构成投资建议。")

    # 保存复盘结果
    try:
        with open(os.path.join(SCRIPT_DIR, "review_report.txt"), "w", encoding="utf-8") as f:
            f.write("交易复盘报告\n")
            f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            f.write(f"信号总数: {len(signals)} | 配对交易: {len(trades)}\n")
            f.write(stats_by_grade(trades) + "\n")
            f.write(stats_by_mktstate(trades) + "\n")
            f.write(stats_by_industry(trades) + "\n")
        p(f"\n💾 复盘报告已保存: {os.path.join(SCRIPT_DIR, 'review_report.txt')}")
    except Exception:
        pass

    # 分段直发到 QQ（QQ 单条消息限长，必须分段）；失败则输出原文由 cron 兜底
    report = "\n".join(out)
    if not qq_send.push_or_stdout(report):
        print(report)


if __name__ == "__main__":
    main()
