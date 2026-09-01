#!/usr/bin/env python3
"""
仓位管理系统 — 止盈止损跟踪 + 每日盈亏 + 总仓位风控 + 交易日志 + 资金分配
数据存储：~/.hermes/scripts/positions.json
"""
import json, os, csv
from datetime import datetime, date

POSITIONS_FILE = os.path.expanduser("~/.hermes/scripts/positions.json")
TRADE_LOG = os.path.expanduser("~/.hermes/scripts/trade_log.csv")
# 资金池（2026-08-06 更新：个股已全部清仓，现金约2.4万）
# 仓位监控口径：总资金 = 持仓投入(约3.1万) + 可用现金(约2.4万) ≈ 5.5万
#   ETF池：持仓31000 + 现金约4000 ≈ 35000；个股池：现金约20000（8-6卖出回款约4100并入）
ETF_TOTAL = 35000
STOCK_TOTAL = 20000

# 已平仓状态标记：status 为这些值的条目不再视为持仓
CLOSED_STATUS = ("sold", "closed")

def load_positions():
    """读取持仓记录（自动过滤已平仓 status=sold/closed 的条目）"""
    if not os.path.isfile(POSITIONS_FILE):
        return {"etf": [], "stock": []}
    try:
        with open(POSITIONS_FILE, "r") as f:
            data = json.load(f)
        for grp in ("etf", "stock"):
            data[grp] = [p for p in data.get(grp, []) if p.get("status") not in CLOSED_STATUS]
        return data
    except:
        return {"etf": [], "stock": []}

def save_positions(data):
    os.makedirs(os.path.dirname(POSITIONS_FILE), exist_ok=True)
    with open(POSITIONS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def build_position_context() -> str:
    """从权威持仓文件动态生成持仓描述（供各脚本 prompt 注入，防止硬编码过期）"""
    data = load_positions()
    lines = []
    for p in data.get("etf", []) + data.get("stock", []):
        name = p.get("name", "")
        code = p.get("code", "")
        buy = p.get("buy_price")
        amt = p.get("amount")
        sl = p.get("stop_loss")
        _sl = f"，固定止损{sl}" if sl else ""
        lines.append(f"{name}({code}) 成本{buy}/投入{amt}元{_sl}")
    if not lines:
        return "当前无持仓（ETF与个股均空仓）"
    return "；".join(lines)

def add_position(code, name, buy_price, amount, type_s="etf", stop_loss=None, take_profit=None):
    """添加一笔买入记录"""
    data = load_positions()
    entry = {
        "code": code, "name": name, "buy_price": buy_price,
        "amount": amount, "quantity": round(amount / buy_price, 0) if buy_price else 0,
        "stop_loss": stop_loss, "take_profit": take_profit,
        "buy_date": date.today().isoformat(),
        "type": type_s,
    }
    data[type_s].append(entry)
    save_positions(data)
    return entry

def remove_position(code, type_s="etf", all_of_code=False):
    """卖出或删除持仓记录"""
    data = load_positions()
    if all_of_code:
        data[type_s] = [p for p in data[type_s] if p["code"] != code]
    else:
        # 移除最近一笔
        for i in range(len(data[type_s]) - 1, -1, -1):
            if data[type_s][i]["code"] == code:
                data[type_s].pop(i)
                break
    save_positions(data)

def close_position(code, type_s="etf", sell_price=None, reason=""):
    """
    平仓：从持仓中移除该代码（含多次建仓的全部条目），可选记录卖出价格并写交易日志。
    返回 (removed_list, total_pnl)；未找到返回 (None, None)。
    """
    data = load_positions()
    removed, total_pnl = [], 0.0
    remaining = []
    for p in data.get(type_s, []):
        if p["code"] == code:
            removed.append(p)
            if sell_price:
                buy = float(p.get("buy_price") or 0)
                amount = float(p.get("amount") or 0)
                shares = p.get("shares") or p.get("quantity")
                if not shares:
                    shares = round(amount / buy) if buy else 0
                try:
                    shares = float(shares)
                except Exception:
                    shares = round(amount / buy) if buy else 0
                total_pnl += round((float(sell_price) - buy) * shares, 2)
        else:
            remaining.append(p)
    if not removed:
        return None, None
    data[type_s] = remaining
    save_positions(data)
    for p in removed:
        log_trade(code, p.get("name", ""), "清仓", p.get("amount", 0),
                  sell_price or "", "—", "—", reason or "平仓")
    return removed, round(total_pnl, 2)


def check_stops(current_prices):
    """
    检查止盈止损是否触发
    current_prices: {code: current_price}
    返回: (stops_triggered, summary)
    """
    data = load_positions()
    triggered = {"stop_loss": [], "take_profit": []}
    total_invested = 0
    total_current = 0
    details = []
    
    for ptype in ["etf", "stock"]:
        for pos in data[ptype]:
            code = pos["code"]
            cur = current_prices.get(code)
            if cur is None:
                continue
            
            buy_price = pos["buy_price"]
            amount = pos["amount"]
            sl = pos.get("stop_loss")
            tp = pos.get("take_profit")
            
            total_invested += amount
            current_value = round(amount / buy_price * cur, 2) if buy_price else amount
            total_current += current_value
            
            pnl = round(current_value - amount, 2)
            pnl_pct = round((cur - buy_price) / buy_price * 100, 2)
            
            # 检查止盈止损
            if sl and cur <= sl:
                triggered["stop_loss"].append({
                    "code": code, "name": pos["name"], "buy": buy_price,
                    "cur": cur, "sl": sl, "loss": pnl_pct, "type": ptype
                })
            elif tp and cur >= tp:
                triggered["take_profit"].append({
                    "code": code, "name": pos["name"], "buy": buy_price,
                    "cur": cur, "tp": tp, "profit": pnl_pct, "type": ptype
                })
            
            details.append({
                "code": code, "name": pos["name"],
                "buy": buy_price, "cur": cur,
                "amount": amount, "value": current_value,
                "pnl": pnl, "pnl_pct": pnl_pct,
                "sl": sl, "tp": tp,
                "type": ptype,
                "date": pos.get("buy_date", "?"),
            })
    
    total_etf = sum(p["amount"] for p in data["etf"])
    total_stock = sum(p["amount"] for p in data["stock"])
    
    # 总仓位风控
    total_capital = ETF_TOTAL + STOCK_TOTAL
    total_used = total_invested
    position_ratio = round(total_used / total_capital * 100, 1)
    risk_warnings = []
    
    if position_ratio > 80:
        risk_warnings.append(f"🔴 总仓位{position_ratio}%超过80%！建议减仓")
    elif position_ratio > 60:
        risk_warnings.append(f"🟡 总仓位{position_ratio}%，注意控制")
    
    # 单只集中度
    for pos in data["etf"] + data["stock"]:
        pct = round(pos["amount"] / total_capital * 100, 1)
        if pct > 20:
            risk_warnings.append(f"⚠️ {pos['name']}占比{pct}%超过20%，集中度偏高")
    
    return triggered, {
        "details": details,
        "total_invested": round(total_invested, 2),
        "total_current": round(total_current, 2),
        "total_pnl": round(total_current - total_invested, 2),
        "total_pnl_pct": round((total_current - total_invested) / total_invested * 100, 2) if total_invested else 0,
        "total_etf": round(total_etf, 2),
        "total_stock": round(total_stock, 2),
        "position_ratio": position_ratio,
        "risk_warnings": risk_warnings,
    }


def generate_risk_report(current_prices):
    """生成风控报告文本（含四重退出系统）"""
    triggered, summary = check_stops(current_prices)
    
    lines = []
    lines.append("\n╔════════════════════════════════╗")
    lines.append("║  🛡️ 仓位风控系统               ║")
    lines.append(f"║  {datetime.now().strftime('%Y-%m-%d %H:%M')}            ║")
    lines.append("╚════════════════════════════════╝")
    
    # 止盈止损预警
    if triggered["stop_loss"] or triggered["take_profit"]:
        lines.append("\n🚨 【止盈止损触发】")
        for t in triggered["stop_loss"]:
            lines.append(f"  🔴 {t['name']}({t['code']}) 触发止损！买入{t['buy']}→现价{t['cur']} 亏损{t['loss']:+.2f}%")
            lines.append(f"     建议：执行止损卖出")
        for t in triggered["take_profit"]:
            lines.append(f"  🟢 {t['name']}({t['code']}) 触止盈！买入{t['buy']}→现价{t['cur']} 盈利{t['profit']:+.2f}%")
            lines.append(f"     建议：止盈落袋或上移止损继续持有")
    
    # 四重退出系统（第五阶段）：对每个持仓独立检查
    exit_triggered, exit_lines = check_exits(current_prices)
    if exit_lines:
        lines.append("\n📤 【四重退出系统】①止损(MA20/买价-2ATR/固定取最高) ②趋势(破MA10减半/破MA20清仓) ③盈利保护(≥10%激活,峰值回撤8%) ④时间退出(10日未涨)")
        lines.extend(exit_lines)
        if exit_triggered:
            lines.append("\n🚨 【退出信号汇总】")
            for e in exit_triggered:
                _icon = {"high": "🔴", "mid": "🟠"}.get(e.get("severity"), "🟡")
                lines.append(f"  {_icon} {e['name']}({e['code']}) {e['kind']} → {e['label']}")
    
    # 持仓盈亏
    if summary["details"]:
        lines.append(f"\n📊 【持仓明细】")
        lines.append(f"  {'名称':<12} {'买入价':>8} {'现价':>8} {'盈亏':>10} {'止损价':>8}")
        lines.append("  " + "-" * 50)
        for d in summary["details"]:
            pnl_str = f"{d['pnl']:+.0f}元({d['pnl_pct']:+.1f}%)"
            ce = "🟢" if d['pnl'] >= 0 else "🔴"
            sl_str = str(d['sl']) if d['sl'] else "—"
            lines.append(f"  {ce} {d['name']:<8} {d['buy']:>8.2f} {d['cur']:>8.2f} {pnl_str:>12} {sl_str:>8}")
    
    # 总盈亏
    lines.append(f"\n💰 【总盈亏】{summary['total_pnl']:+.0f}元 ({summary['total_pnl_pct']:+.2f}%)")
    
    # 仓位风控
    lines.append(f"\n📐 【仓位监控】")
    lines.append(f"  ETF占用:{summary['total_etf']:.0f}元  个股占用:{summary['total_stock']:.0f}元")
    lines.append(f"  总投资:{summary['total_invested']:.0f}元/{ETF_TOTAL+STOCK_TOTAL}元 = {summary['position_ratio']}%")
    
    for w in summary["risk_warnings"]:
        lines.append(f"  {w}")
    
    if not summary["risk_warnings"] and summary["position_ratio"] < 50:
        lines.append(f"  🟢 仓位健康")

    lines.append("\n⚠️ 风控信号仅供参考")
    lines.append("🧾 手续费硬性规则：佣金万2.5最低5元/笔，单笔<3000元占比≥0.17%不划算；做T价差必须覆盖双边手续费(≥10元)")
    return "\n".join(lines)


# ==================== 交易日志与信号统计 ====================

def log_trade(code, name, action, amount, price, grade, score, reason):
    """记录每次交易"""
    os.makedirs(os.path.dirname(TRADE_LOG), exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    file_exists = os.path.isfile(TRADE_LOG)
    with open(TRADE_LOG, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["时间","代码","名称","操作","金额","价格","信号等级","评分","原因"])
        writer.writerow([now, code, name, action, amount, price, grade, score, reason])

def get_signal_stats():
    """统计各信号等级的胜率"""
    if not os.path.isfile(TRADE_LOG):
        return None
    trades = []
    with open(TRADE_LOG, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append(row)
    if len(trades) < 2:
        return None
    
    # 按信号等级分组统计
    stats = {"S": {"buys": 0, "sells": 0, "pnl": 0},
             "A": {"buys": 0, "sells": 0, "pnl": 0},
             "B": {"buys": 0, "sells": 0, "pnl": 0}}
    
    # 简易配对: 同一标的的买入-卖出配对
    for grade in ["S","A","B"]:
        grade_buys = [t for t in trades if t.get("信号等级","") == grade and t["操作"] == "加仓"]
        grade_sells = [t for t in trades if t.get("信号等级","") == grade and t["操作"] == "减仓"]
        stats[grade]["buys"] = len(grade_buys)
        stats[grade]["sells"] = len(grade_sells)
    
    total_trades = len(trades)
    
    return {
        "total": total_trades,
        "stats": stats,
        "latest": trades[-5:] if len(trades) >= 5 else trades,
    }

def generate_signal_stats_report():
    """生成信号统计报告"""
    s = get_signal_stats()
    if not s:
        return "\n📊 【信号统计】暂无交易记录\n"
    
    lines = ["\n📊 【信号胜率统计】"]
    lines.append(f"  {'等级':<6} {'买入次数':>8} {'卖出次数':>8} {'状态':>10}")
    lines.append("  " + "-" * 35)
    
    for grade in ["S","A","B"]:
        st = s["stats"][grade]
        status = "✅ 有交易" if st["sells"] > 0 else "⏳ 只有买入" if st["buys"] > 0 else "—"
        lines.append(f"  {grade}级     {st['buys']:>6}次     {st['sells']:>6}次     {status:>8}")
    
    lines.append(f"  总交易:{s['total']}笔")
    return "\n".join(lines)


# ==================== 资金分配 ====================

def _lot_unit(code):
    """ETF(51/56/58/15/16开头)按份，股票按股"""
    return "份" if code.startswith(("51", "56", "58", "15", "16")) else "股"

def _round_lot(amount, price, remaining):
    """金额→整手(100股/份)金额；返回 (整手金额, 手数) 或 None(连1手都买不起)"""
    if not price or price <= 0:
        return (amount, 0)
    shares = max(100, int(amount / price / 100) * 100)
    while shares > 100 and shares * price > remaining:
        shares -= 100
    if shares * price > remaining:
        return None
    return (int(shares * price), shares)

def allocate_capital(buy_signals, available_capital):
    """
    多信号出现时的资金分配（按100股/100份整手取整）
    buy_signals: [(code, name, score, grade, price), ...]
    available_capital: 可用资金
    
    返回: [(code, name, amount, reason), ...]
    """
    if not buy_signals or available_capital < 1000:
        return []
    
    # 按等级分组
    s_signals = [s for s in buy_signals if s[3] == "S"]
    a_signals = [s for s in buy_signals if s[3] == "A"]
    b_signals = [s for s in buy_signals if s[3] == "B"]

    # 手续费硬性规则（2026-08-06）：单笔<3000元佣金5元占比≥0.17%不划算，低于3000不分配
    MIN_ALLOC = 3000

    allocations = []
    remaining = available_capital

    # S级：分配50%
    if s_signals and remaining > 0:
        s_pool = min(int(available_capital * 0.5), remaining)
        total_s_score = sum(abs(s[2]) for s in s_signals)
        for code, name, score, grade, price in s_signals:
            share = int(s_pool * abs(score) / total_s_score) if total_s_score else 0
            if share >= MIN_ALLOC:
                unit = _lot_unit(code)
                rounded = _round_lot(share, price, remaining)
                if not rounded:
                    continue
                share, shares = rounded
                allocations.append((code, name, share, f"S级评分{score:+d} 优先买入 ≈{shares}{unit}(手续费≈{round(max(share*0.00025,5)+share*0.00001,1)}元)"))
                remaining -= share

    # A级：分配30%
    if a_signals and remaining > 0:
        a_pool = min(int(available_capital * 0.3), remaining)
        total_a_score = sum(abs(s[2]) for s in a_signals)
        for code, name, score, grade, price in a_signals:
            share = int(a_pool * abs(score) / total_a_score) if total_a_score else 0
            if share >= MIN_ALLOC:
                unit = _lot_unit(code)
                rounded = _round_lot(share, price, remaining)
                if not rounded:
                    continue
                share, shares = rounded
                allocations.append((code, name, share, f"A级评分{score:+d} ≈{shares}{unit}(手续费≈{round(max(share*0.00025,5)+share*0.00001,1)}元)"))
                remaining -= share

    # B级：分配20%
    if b_signals and remaining > 0:
        b_pool = min(int(available_capital * 0.2), remaining)
        total_b_score = sum(abs(s[2]) for s in b_signals)
        for code, name, score, grade, price in b_signals:
            share = int(b_pool * abs(score) / total_b_score) if total_b_score else 0
            if share >= MIN_ALLOC:
                unit = _lot_unit(code)
                rounded = _round_lot(share, price, remaining)
                if not rounded:
                    continue
                share, shares = rounded
                allocations.append((code, name, share, f"B级评分{score:+d} 轻仓试探 ≈{shares}{unit}(手续费≈{round(max(share*0.00025,5)+share*0.00001,1)}元)"))
                remaining -= share
    
    return allocations

def generate_allocation_report(buy_signals, available_capital):
    """生成资金分配报告"""
    allocs = allocate_capital(buy_signals, available_capital)
    if not allocs:
        return ""
    
    total_alloc = sum(a[2] for a in allocs)
    lines = ["\n💎 【资金分配建议】"]
    lines.append(f"  可用资金:{available_capital}元  建议分配:{total_alloc}元  剩余:约{available_capital-total_alloc}元")
    lines.append(f"  {'标的':<14} {'建议金额':>8} {'比例':>6} {'理由'}")
    lines.append("  " + "-" * 50)
    
    for code, name, amount, reason in allocs:
        pct = round(amount / available_capital * 100, 1)
        lines.append(f"  {name:<10} {amount:>6}元 {pct:>5}% {reason}")
    
    return "\n".join(lines)


# ==================== 四重退出系统（第五阶段 2026-08-05）====================
# 与 short_term.py 的 build_exit_plan 算法完全一致，独立实现供盘后风控报告复用：
# ① 止损退出：止损线=max(MA20, 买入价-2×ATR14, 固定止损) → 破线无条件清仓
# ② 趋势退出：盈利持仓上涨中不提前卖；破MA10减仓50%；破MA20清仓
# ③ 盈利保护：盈利≥10%激活，自买入后最高价回撤8% → 卖出
# ④ 时间退出：持有≥10个交易日未上涨 → 卖出

def _sina_kline(code, days=120):
    """获取日K线（新浪），返回 [{day,high,low,close,volume},...]；失败返回[]"""
    try:
        import json as _json
        import urllib.request as _ur
        pref = "sh" if code.startswith(("5", "6", "9")) else "sz"
        url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"CN_MarketData.getKLineData?symbol={pref}{code}&scale=240&ma=5&datalen={days}")
        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
        return _json.loads(_ur.urlopen(req, timeout=10).read().decode("gbk"))
    except Exception:
        return []

def _calc_ma(closes, n):
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 3)

def _calc_atr(highs, lows, closes, n=14):
    """ATR14：平均真实波幅"""
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    return round(sum(trs[-n:]) / n, 3)

def trapped_codes():
    """套牢盘代码集合（唯一来源：positions.json 中 no_sell=true 的持仓条目；清仓后自动消失）"""
    try:
        data = load_positions()
        return {p["code"] for grp in ("etf", "stock") for p in data.get(grp, []) if p.get("no_sell")}
    except Exception:
        return set()

def is_trapped(code):
    return code in trapped_codes()

def _exit_sell_label(code, amount, cur, ratio=1.0, unit="份", shares=None):
    """触发卖出的手数描述；套牢盘改为提示不清仓

    2026-08-26 修复：positions.json 的 amount 是买入成本金额，不是当前市值，
    旧实现用 amount/cur 反推份额 → 盈利时少报（159858 清仓2500被说成减仓2200）、
    亏损时超报（588750 持仓2900被说成减仓3300 超卖400份）。
    优先使用真实 shares 字段；无 shares 时回退旧逻辑（amount=市值语义兼容）。
    """
    if code in trapped_codes():
        return "套牢盘禁清仓→反弹减仓/做T处理"
    if not cur or cur <= 0:
        return f"~{int(amount * ratio)}元"
    if shares:
        held_shares = int(shares)
    else:
        held_shares = int(amount / cur)
    sell = max(100, int(held_shares * ratio / 100) * 100)
    if held_shares > 0 and sell >= held_shares:
        return f"清仓(约{int(held_shares*cur)}元)"
    if not shares and held_shares > 0 and sell * cur >= amount * 0.98:
        return f"清仓(约{int(held_shares*cur)}元)"
    return f"减仓{sell}{unit}(约{int(sell*cur)}元)"

def build_exit_plan(pos, cur, kline, no_sell=None):
    """
    单只持仓的四重退出信号（唯一算法实现，short_term.py 委托调用本函数）
    pos: positions.json 持仓记录 {code,name,buy_price,buy_date,amount,stop_loss}
    cur: 当前价；kline: 日K线
    no_sell: 套牢盘标记（True=只作反弹减仓/做T提示）；None 时按 trapped_codes() 自动判断
    返回 (lines, triggered)：lines输出行；triggered退出信号列表
    """
    lines, triggered = [], []
    buy_price = pos.get("buy_price")
    if not buy_price or not cur or not kline:
        return lines, triggered
    buy_price = float(buy_price)
    buy_date = str(pos.get("buy_date", ""))
    code = pos.get("code", "")
    name = pos.get("name", "")
    amount = pos.get("amount", 0) or 0
    fixed_stop = pos.get("stop_loss")
    try:
        fixed_stop = float(fixed_stop) if fixed_stop else None
    except:
        fixed_stop = None

    closes = [float(k["close"]) for k in kline]
    highs = [float(k["high"]) for k in kline]
    lows = [float(k["low"]) for k in kline]
    ma10, ma20 = _calc_ma(closes, 10), _calc_ma(closes, 20)
    atr14 = _calc_atr(highs, lows, closes, 14)

    # 持有天数（交易日，day>=买入日的K线数）与买入后最高价
    days_held, peak = 0, cur
    _started = not buy_date
    for _k in kline:
        _d = str(_k.get("day", ""))
        if buy_date and _d >= buy_date:
            _started = True
        if _started:
            days_held += 1
            peak = max(peak, float(_k["high"]))
    if not buy_date:
        peak = max([float(_k["high"]) for _k in kline] + [cur])
        days_held = len(kline)

    pnl_pct = round((cur - buy_price) / buy_price * 100, 2) if buy_price else 0
    atr_stop = round(buy_price - 2 * atr14, 3) if atr14 else None
    _cands = [x for x in (fixed_stop, ma20, atr_stop) if x]
    stop_line = round(max(_cands), 3) if _cands else None
    # 防洗盘确认（2026-08-25 立霸案例：开盘急杀-6%瞬间击穿后V型拉回，瞬时破位≠真破位）：
    # 盘中瞬时击穿止损线但现价已收回线上 → 降级为预警不执行（收盘价跌破才清仓）；
    # 今日最低取日K最后一根（盘中=实时最低，盘后=当日最低），无数据时退化为原逻辑。
    today_low = None
    if kline:
        try:
            today_low = float(kline[-1].get("low"))
        except (TypeError, ValueError):
            today_low = None
    stop_hit = stop_line is not None and cur <= stop_line
    fake_break = (not stop_hit and stop_line is not None and today_low is not None
                  and today_low <= stop_line and cur > stop_line)
    if no_sell is None:
        no_sell = code in trapped_codes()
    unit = _lot_unit(code)

    def _sell(ratio=1.0):
        return _exit_sell_label(code, amount, cur, ratio, unit, shares=pos.get("shares"))

    lines.append("  📤 【退出系统】")
    # ① 止损退出
    _src = []
    if ma20: _src.append(f"MA20={ma20:.3f}")
    if atr_stop: _src.append(f"买价-2ATR={atr_stop:.3f}")
    if fixed_stop: _src.append(f"固定={fixed_stop:.3f}")
    if stop_line:
        _low_note = f"，今日最低{today_low}" if today_low is not None else ""
        if stop_hit:
            lines.append(f"  🛑 止损退出: 已触发！现价{cur} ≤ 止损线{stop_line} ({'+'.join(_src)}) → {_sell(1.0)}")
            triggered.append({"kind": "止损退出", "severity": "high", "label": _sell(1.0), "code": code, "name": name})
        elif fake_break:
            lines.append(f"  ⚠️ 止损线: {stop_line} ({'+'.join(_src)}取最高) — 盘中曾破位(最低{today_low})但现价{cur}已收回线上 → 疑似洗盘，不执行；收盘价跌破才清仓")
        else:
            lines.append(f"  🛑 止损线: {stop_line} ({'+'.join(_src)}取最高) — 破线无条件清仓，当前未触发{_low_note}")
    # ② 趋势退出（仅盈利/上涨持仓启用）
    if not no_sell and pnl_pct > 0 and ma20 and cur > ma20:
        _fake_ma10 = (today_low is not None and ma10 is not None
                      and today_low <= ma10 and cur > ma10)
        if ma10 and cur <= ma10:
            lines.append(f"  📉 趋势退出: 收盘破MA10({ma10:.3f}) → 减仓50% ({_sell(0.5)})")
            triggered.append({"kind": "趋势退出减仓", "severity": "mid", "label": _sell(0.5), "code": code, "name": name})
        elif _fake_ma10:
            lines.append(f"  📉 趋势退出: MA10({ma10:.3f})盘中曾破位(最低{today_low})已收回(现价{cur}) → 疑似洗盘，收盘确认；收盘破MA10减半/破MA20清仓")
        else:
            lines.append(f"  📉 趋势退出: 上涨中不提前卖；收盘破MA10({ma10 and round(ma10,3)})减半 / 破MA20({ma20:.3f})清仓")
    elif not no_sell and ma20 and cur <= ma20 and pnl_pct > 0 and not stop_hit:
        lines.append(f"  📉 趋势退出: 已破MA20({ma20:.3f}) → 清仓 ({_sell(1.0)})")
        triggered.append({"kind": "趋势退出清仓", "severity": "high", "label": _sell(1.0), "code": code, "name": name})
    # ③ 盈利保护
    if pnl_pct >= 10:
        protect_line = round(peak * 0.92, 3)
        if cur <= protect_line:
            lines.append(f"  🛡️ 盈利保护: 已触发！盈利{pnl_pct:+.1f}%≥10%，自峰值{peak:.3f}回撤8%破{protect_line} → {_sell(1.0)}")
            triggered.append({"kind": "盈利保护", "severity": "high", "label": _sell(1.0), "code": code, "name": name})
        else:
            lines.append(f"  🛡️ 盈利保护: 盈利{pnl_pct:+.1f}%已激活，峰值{peak:.3f}，回撤8%破{protect_line}卖出（当前安全）")
    else:
        lines.append(f"  🛡️ 盈利保护: 盈利{pnl_pct:+.1f}%（<10%未激活，激活后自峰值回撤8%卖出）")
    # ④ 时间退出
    if days_held >= 10 and pnl_pct <= 0:
        lines.append(f"  ⏰ 时间退出: 持有{days_held}日未上涨(资金效率低) → {'反弹减仓' if no_sell else _sell(1.0)}")
        triggered.append({"kind": "时间退出", "severity": "mid", "label": _sell(1.0), "code": code, "name": name})
    else:
        _note = "已盈利不受限" if pnl_pct > 0 else f"再等{max(0,10-days_held)}日"
        lines.append(f"  ⏰ 时间退出: 持有{days_held}日，10日未上涨则卖（当前{_note}）")
    return lines, triggered

def check_exits(current_prices):
    """
    对所有持仓跑四重退出（盘后风控用）
    current_prices: {code: current_price}
    返回 (triggered, lines)：triggered触发信号列表；lines输出行
    """
    data = load_positions()
    all_triggered, lines = [], []
    for ptype in ["etf", "stock"]:
        for pos in data[ptype]:
            code = pos["code"]
            cur = current_prices.get(code)
            if cur is None:
                continue
            kline = _sina_kline(code)
            if not kline:
                continue
            elines, etrig = build_exit_plan(pos, cur, kline)
            if elines:
                lines.append(f"\n  📤 {pos['name']}({code}) 现价{cur}")
                lines.extend(elines)
            all_triggered.extend(etrig)
    return all_triggered, lines
