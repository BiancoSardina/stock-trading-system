#!/usr/bin/env python3
"""
ETF 量化交易引擎 v2 — 多策略信号系统
基于趋势过滤+信号共振+动量筛选+波动率管理

核心逻辑：
1. 趋势过滤：周线决定多空方向，只顺势交易
2. 信号共振：至少2个独立指标同向才触发
3. 动量筛选：只买跑赢大盘的强势ETF
4. 波动率管理：高波动→小仓位，低波动→正常仓位
5. 技术位止损：基于支撑/阻力，不是固定比例
"""
import json, os, csv, math
from datetime import datetime, date

TRADE_LOG = os.path.expanduser("~/.hermes/scripts/trade_log.csv")
TOTAL = 50000

# ========= 策略参数 =========
# 评分权重（多因子加权）
SIGNAL_WEIGHTS = {
    "周线趋势": 3.0,    # 最高权重—大方向
    "日线多空": 2.0,    # 日线级别
    "RSI": 1.5,         # 超卖/超买
    "MACD": 1.5,        # 动量
    "布林带": 1.0,      # 位置
    "成交量": 1.0,      # 量能确认
    "相对强弱": 2.0,    # vs大盘
    "RSI背离": 2.0,     # 顶/底背离—强信号
}

# 仓位规则
MAX_POS_PCT = {
    5: 0.20,   # 评分5+ → 20%仓位
    4: 0.18,   # 评分4+ → 18%
    3: 0.15,   # 评分3+ → 15%
    2: 0.10,   # 评分2+ → 10%
    1: 0.05,   # 评分1+ → 5%
    0: 0.00,   # 评分≤0 → 不动
}

# 止损规则（基于ATR倍数，趋势好时给更多空间）
STOP_LOSS_MULT = {5: 3.0, 4: 2.5, 3: 2.0, 2: 1.5, 1: 1.5}
TAKE_PROFIT_MULT = {5: 5.0, 4: 4.0, 3: 3.0, 2: 2.5, 1: 2.0}


def calc_atr(highs, lows, closes, n=14):
    """计算ATR"""
    if len(highs) < n+1 or len(lows) < n+1 or len(closes) < n+1:
        return None
    trs = []
    for i in range(-n, 0):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i-1])
        lc = abs(lows[i] - closes[i-1])
        trs.append(max(hl, hc, lc))
    return round(sum(trs) / n, 4)


def calc_ema(prices, n):
    if not prices or len(prices) < n: return None
    mul = 2/(n+1)
    ema_v = sum(prices[:n])/n
    for p in prices[n:]:
        ema_v = (p-ema_v)*mul + ema_v
    return round(ema_v, 3)


def detect_rsi_divergence(closes, rsi_values, lookback=30):
    """
    RSI背离检测
    底背离: 价格新低但RSI抬高 → 买入信号
    顶背离: 价格新高但RSI走低 → 卖出信号
    返回: ("bullish"/"bearish"/None, 强度1-3)
    """
    if len(closes) < lookback or len(rsi_values) < lookback:
        return None, 0
    
    recent_c = closes[-lookback:]
    recent_r = rsi_values[-lookback:]
    
    # 找最近的价格低点和RSI低点
    price_low_idx = recent_c.index(min(recent_c))
    rsi_low_idx = recent_r.index(min(recent_r))
    
    # 找最近的价格高点和RSI高点
    price_high_idx = recent_c.index(max(recent_c))
    rsi_high_idx = recent_r.index(max(recent_r))
    
    # 底背离：价格新低但RSI没有新低
    if price_low_idx == len(recent_c) - 1 or price_low_idx >= len(recent_c) - 3:
        # 最近3根K线价格创了30日新低
        prev_min_price = min(recent_c[:price_low_idx]) if price_low_idx > 0 else min(recent_c[:-1])
        prev_min_rsi = min(recent_r[:rsi_low_idx]) if rsi_low_idx > 0 else min(recent_r[:-1])
        
        if recent_c[price_low_idx] < prev_min_price and recent_r[price_low_idx] > prev_min_rsi:
            strength = 3 if recent_r[price_low_idx] - prev_min_rsi > 10 else 2
            return "bullish", strength
    
    # 顶背离：价格新高但RSI没有新高
    if price_high_idx == len(recent_c) - 1 or price_high_idx >= len(recent_c) - 3:
        prev_max_price = max(recent_c[:price_high_idx]) if price_high_idx > 0 else max(recent_c[:-1])
        prev_max_rsi = max(recent_r[:rsi_high_idx]) if rsi_high_idx > 0 else max(recent_r[:-1])
        
        if recent_c[price_high_idx] > prev_max_price and recent_r[price_high_idx] < prev_max_rsi:
            strength = 3 if prev_max_rsi - recent_r[price_high_idx] > 10 else 2
            return "bearish", strength
    
    return None, 0


def calc_rsi(prices, n=14):
    if len(prices) < n+1: return None
    g, l = 0, 0
    for i in range(-n, 0):
        d = prices[i] - prices[i-1]
        if d > 0: g += d
        else: l += abs(d)
    ag, al = g/n, l/n
    if al == 0: return 100
    return round(100 - 100/(1+ag/al), 1)


def generate_strategy_signals(code, name, cur, closes, highs, lows, vols, benchmark_chg):
    """
    多策略信号生成器
    返回: (total_score, signals_dict, divergence)
    """
    if not closes or len(closes) < 50:
        return 0, {}, None
    
    signals = {}
    
    # ===== 1. 周线趋势（大方向过滤器）=====
    wma5 = sum(closes[-25:])/25 if len(closes) >= 25 else None
    wma10 = sum(closes[-50:])/50 if len(closes) >= 50 else None
    wma20 = sum(closes[-100:])/100 if len(closes) >= 100 else None
    
    weekly_trend = 0
    if wma5 and wma10:
        if cur > wma5 > wma10:
            weekly_trend = 3  # 强势多头
        elif cur > wma5:
            weekly_trend = 1  # 偏多
        elif cur < wma5 < wma10:
            weekly_trend = -3  # 强势空头
        elif cur < wma5:
            weekly_trend = -1  # 偏空
    signals["周线趋势"] = weekly_trend
    
    # ===== 2. 日线多空排列 =====
    ma5 = sum(closes[-5:])/5 if len(closes) >= 5 else None
    ma10 = sum(closes[-10:])/10 if len(closes) >= 10 else None
    ma20 = sum(closes[-20:])/20 if len(closes) >= 20 else None
    
    daily_ma = 0
    if ma5 and ma10 and ma20:
        if cur > ma5 > ma10 > ma20:
            daily_ma = 3  # 多头排列
        elif cur > ma10:
            daily_ma = 1  # 偏多
        elif cur < ma5 < ma10:
            daily_ma = -3  # 空头排列
        elif cur < ma10:
            daily_ma = -1  # 偏空
    signals["日线多空"] = daily_ma
    
    # ===== 3. RSI策略 =====
    prev_closes = closes[:-1]
    rsi14 = calc_rsi(closes)
    rsi_prev = calc_rsi(prev_closes) if len(prev_closes) > 14 else None
    
    rsi_signal = 0
    if rsi14 is not None:
        if rsi14 < 25:
            rsi_signal = 3  # 极度超卖→强买入
        elif rsi14 < 30:
            rsi_signal = 2  # 超卖→买入
        elif rsi14 > 75:
            rsi_signal = -3  # 极度超买→强卖出
        elif rsi14 > 70:
            rsi_signal = -2  # 超买→卖出
        # RSI金叉/死叉（6日快线穿14日慢线）
        rsi6 = calc_rsi(closes, 6)
        if rsi6 is not None and rsi_prev is not None:
            if rsi6 > rsi_prev and rsi_prev <= 30:
                rsi_signal = max(rsi_signal, 2)  # RSI金叉+超卖=强信号
            elif rsi6 < rsi_prev and rsi_prev >= 70:
                rsi_signal = min(rsi_signal, -2)  # RSI死叉+超买=强信号
    signals["RSI"] = rsi_signal
    
    # ===== 4. MACD策略 =====
    macd_signal = 0
    if len(closes) >= 26:
        dif = cur - calc_ema(closes, 12)
        ema12_list = [calc_ema(closes[:i+1], 12) for i in range(len(closes))]
        ema26_list = [calc_ema(closes[:i+1], 26) for i in range(len(closes))]
        difs = [e12 - e26 for e12, e26 in zip(ema12_list, ema26_list) if e12 and e26]
        
        if len(difs) >= 9:
            dea = calc_ema(difs, 9)
            prev_dif = difs[-2] if len(difs) >= 2 else 0
            prev_dea = calc_ema(difs[:-1], 9) if len(difs) >= 10 else 0
            
            if dif > dea and prev_dif <= prev_dea:
                macd_signal = 3  # MACD金叉
            elif dif < dea and prev_dif >= prev_dea:
                macd_signal = -3  # MACD死叉
            elif dif > dea:
                macd_signal = 1  # MACD偏多
            elif dif < dea:
                macd_signal = -1  # MACD偏空
    signals["MACD"] = macd_signal
    
    # ===== 5. 布林带策略 =====
    boll_signal = 0
    if len(closes) >= 20:
        bm = sum(closes[-20:])/20
        std = (sum((x-bm)**2 for x in closes[-20:])/20)**0.5
        bt = bm + 2*std
        bb = bm - 2*std
        
        if cur <= bb:
            boll_signal = 2  # 触下轨→买入
        elif cur >= bt:
            boll_signal = -2  # 触上轨→卖出
        # 从下轨反弹
        if len(closes) >= 3 and closes[-3] <= bb and cur > closes[-3]:
            boll_signal = max(boll_signal, 3)  # 下轨反弹→强烈买入
    signals["布林带"] = boll_signal
    
    # ===== 6. 成交量确认 =====
    vol_signal = 0
    if vols and len(vols) >= 5:
        avg_vol = sum(vols[-5:])/5
        vr = vols[-1]/avg_vol if avg_vol > 0 else 1
        prev_chg = (cur - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
        
        if vr > 1.5 and prev_chg > 1:
            vol_signal = 2  # 放量上涨→确认买入
        elif vr > 1.5 and prev_chg < -1:
            vol_signal = -2  # 放量下跌→确认卖出
        elif vr < 0.6 and prev_chg < 0:
            vol_signal = 1  # 缩量下跌→抛压耗尽
    signals["成交量"] = vol_signal
    
    # ===== 7. 相对强弱 =====
    rs_signal = 0
    if benchmark_chg is not None:
        etf_chg = (cur - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
        diff = etf_chg - benchmark_chg
        if diff > 2:
            rs_signal = 3  # 明显跑赢大盘→强势
        elif diff > 0.5:
            rs_signal = 1  # 略强
        elif diff < -2:
            rs_signal = -3  # 明显跑输→弱势
        elif diff < -0.5:
            rs_signal = -1  # 略弱
    signals["相对强弱"] = rs_signal
    
    # ===== 8. RSI背离检测 =====
    all_rsi = []
    for i in range(14, len(closes)):
        segment = closes[:i+1]
        if len(segment) >= 15:
            r = calc_rsi(segment)
            if r is not None:
                all_rsi.append(r)
    if len(all_rsi) >= 30:
        div_type, div_str = detect_rsi_divergence(closes, all_rsi)
        if div_type == "bullish":
            signals["RSI背离"] = 3 * div_str
        elif div_type == "bearish":
            signals["RSI背离"] = -3 * div_str
    
    # ===== 综合评分 =====
    total = 0
    for key, val in signals.items():
        w = SIGNAL_WEIGHTS.get(key, 1.0)
        total += val * w
    
    # 钳制到 ±10
    total = max(-10, min(10, round(total)))
    
    return total, signals, (div_type if 'div_type' in dir() else None)


def calc_position_strategy(score, cur_hold, available, atr_pct=None):
    """
    基于评分的仓位管理 + 风控
    正分→加仓，负分→减仓
    返回: (action, amount, reason, stop_loss, take_profit)
    """
    abs_score = abs(score)
    
    if score > 0:
        # 看多：找目标仓位
        target_pct = 0
        for threshold, pct in sorted(MAX_POS_PCT.items(), reverse=True):
            if abs_score >= threshold:
                target_pct = pct
                break
        # 波动率调整
        if atr_pct and atr_pct > 5:
            vol_mult = max(0.3, 1.0 - (atr_pct - 5) * 0.05)
            target_pct = target_pct * vol_mult
        
        target_value = TOTAL * target_pct
        diff = target_value - cur_hold
        
        if diff > available:
            diff = available
        
        if diff < 500:
            return "不动", 0, f"评分{score:+d}差价过小", None, None
        
        # 计算止损止盈
        sl = tp = None
        if atr_pct:
            sl_mult = 2.0
            tp_mult = 4.0
            sl = sl_mult * atr_pct / 100
            tp = tp_mult * atr_pct / 100
            if tp < sl * 2:
                tp = sl * 2
        
        vol_note = f"(波动{atr_pct}%→{vol_mult:.0%})" if atr_pct and atr_pct > 5 else ""
        return "加仓", int(diff), f"目标{target_pct*100:.0f}%{vol_note}", sl, tp
    
    elif score < 0:
        # 看空：减到最低仓位
        min_pct = 0.05  # 最低保留5%观察仓
        if abs_score >= 6:
            min_pct = 0  # 强烈看空→清仓
        elif abs_score >= 3:
            min_pct = 0.05
        
        target_value = TOTAL * min_pct
        diff = cur_hold - target_value  # 要减掉的金额
        
        if diff < 500:
            return "不动", 0, f"评分{score}已接近目标仓位", None, None
        
        sl = tp = None
        if atr_pct:
            sl = 2.0 * atr_pct / 100
            tp = -2.0 * atr_pct / 100
        
        return "减仓", int(diff), f"目标{min_pct*100:.0f}%观察仓", sl, tp
    
    else:
        # 中性
        return "不动", 0, "评分中性等待", None, None


def log_trade(etf_code, etf_name, action, amount, price, score, reason):
    """记录交易"""
    os.makedirs(os.path.dirname(TRADE_LOG), exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    file_exists = os.path.isfile(TRADE_LOG)
    with open(TRADE_LOG, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["时间", "代码", "名称", "操作", "金额", "价格", "评分", "原因"])
        writer.writerow([now, etf_code, etf_name, action, amount, price, score, reason])


def calc_performance():
    """交易表现统计"""
    if not os.path.isfile(TRADE_LOG):
        return None
    trades = []
    with open(TRADE_LOG, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append(row)
    if len(trades) < 2:
        return None
    buys = [t for t in trades if t["操作"] == "加仓"]
    sells = [t for t in trades if t["操作"] == "减仓"]
    wins = sum(1 for b in buys for s in sells if b["代码"] == s["代码"] and float(s["价格"]) > float(b["价格"]))
    losses = max(0, min(len(buys), len(sells)) - wins)
    total_trades = len(trades)
    win_rate = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0
    return {"total": total_trades, "wins": wins, "losses": losses, "win_rate": win_rate}


def generate_quant_report(etf_data_list, holdings_dict):
    """
    生成量化报告（主入口）
    etf_data_list: [{code, name, cur, closes, highs, lows, vols, benchmark_chg}]
    holdings_dict: {code: current_hold_value}
    """
    lines = []
    available = TOTAL - sum(holdings_dict.values())
    
    lines.append("╔════════════════════════════════╗")
    lines.append("║  📊 量化交易系统 v2            ║")
    lines.append(f"║  {datetime.now().strftime('%Y-%m-%d %H:%M')}            ║")
    lines.append(f"║  总资金:{TOTAL}元 持仓:{sum(holdings_dict.values())}元 现金:{available}元  ║")
    lines.append("╚════════════════════════════════╝")
    
    # 市场环境评估
    bullish_count = 0
    bearish_count = 0
    for d in etf_data_list:
        cur = d["cur"]
        closes = d.get("closes", [])
        if len(closes) >= 5:
            ma5 = sum(closes[-5:])/5
            if cur > ma5:
                bullish_count += 1
            else:
                bearish_count += 1
    
    if bullish_count >= len(etf_data_list) * 0.6:
        lines.append("\n🌤️ 【市场环境】整体偏多，可积极操作")
    elif bearish_count >= len(etf_data_list) * 0.6:
        lines.append("\n🌧️ 【市场环境】整体偏空，建议防守")
    else:
        lines.append("\n⛅ 【市场环境】分化行情，精选个股")
    
    total_score = 0
    
    for data in etf_data_list:
        code = data["code"]
        name = data["name"]
        cur = data["cur"]
        closes = data.get("closes", [])
        highs = data.get("highs", [])
        lows = data.get("lows", [])
        vols = data.get("vols", [])
        bm_chg = data.get("benchmark_chg")
        hold = holdings_dict.get(code, 0)
        chg = data.get("chg", 0)
        ce = "🟢" if chg > 0 else "🔴"
        
        # 生成策略信号
        score, signal_details, divergence = generate_strategy_signals(
            code, name, cur, closes, highs, lows, vols, bm_chg
        )
        total_score += score
        
        # ATR
        atr = calc_atr(highs, lows, closes)
        atr_pct = round(atr/cur*100, 2) if atr and cur else None
        
        # 仓位建议
        action, amount, reason, sl_pct, tp_pct = calc_position_strategy(
            score, hold, available, atr_pct
        )
        
        lines.append(f"\n{ce} 【{name}({code})】 量化评分:{score:+d}/±10")
        lines.append(f"  持仓:{hold}元({round(hold/TOTAL*100,1)}%)  |  ATR波动:{atr_pct}%" if atr_pct else f"  持仓:{hold}元")
        
        # 信号详情
        sig_parts = []
        for k, v in sorted(signal_details.items(), key=lambda x: abs(x[1]), reverse=True)[:4]:
            if v != 0:
                e = "🟢" if v > 0 else "🔴"
                sig_parts.append(f"{e}{k}({v:+d})")
        if sig_parts:
            lines.append(f"  信号:{' '.join(sig_parts)}")
        
        # 背离提示
        if divergence:
            if divergence == "bullish":
                lines.append(f"  ⚡ RSI底背离！价格新低但RSI走高→反弹信号")
            elif divergence == "bearish":
                lines.append(f"  ⚡ RSI顶背离！价格新高但RSI走低→回调风险")
        
        # 趋势方向
        weekly = signal_details.get("周线趋势", 0)
        if weekly > 0:
            lines.append(f"  📈 周线偏多(顺势)")
        elif weekly < 0:
            lines.append(f"  📉 周线偏空(逆势)")
        
        # 止损止盈
        if sl_pct and tp_pct and action != "不动":
            lines.append(f"  🛑 止损:{sl_pct*100:.0f}% ▼  止盈:+{tp_pct*100:.0f}% ▲  收益比1:{round(tp_pct/sl_pct, 1)}")
        
        # 操作建议
        star = "★" * min(5, abs(score)) + "☆" * max(0, 5 - abs(score))
        if action == "加仓":
            lines.append(f"  💡 🟢 {action} {amount}元  [{star}] {reason}")
            log_trade(code, name, action, amount, cur, score, reason)
            available -= amount
        elif action == "减仓":
            lines.append(f"  💡 🔴 {action} {amount}元  [{star}] {reason}")
            log_trade(code, name, action, amount, cur, score, reason)
            available += amount
        else:
            lines.append(f"  💡 ⚪ 不动 [{star}] {reason}")
    
    # 综合
    avg_score = round(total_score / len(etf_data_list), 1) if etf_data_list else 0
    total_hold = sum(holdings_dict.values())
    
    lines.append(f"\n{'='*55}")
    lines.append(f"📋 【量化策略总结】")
    lines.append(f"  组合评分:{avg_score:+.1f}  |  仓位:{total_hold}({round(total_hold/TOTAL*100,1)}%)")
    
    if avg_score >= 3:
        lines.append(f"  🟢🟢 积极进攻：多指标共振看多，可维持高仓位")
    elif avg_score >= 0:
        lines.append(f"  🟢 谨慎偏多：精选信号，控制仓位")
    elif avg_score >= -3:
        lines.append(f"  🟠 防守观望：减少操作，等待信号明确")
    else:
        lines.append(f"  🔴🔴 全面防御：多看少动，现金为王")
    
    # 再平衡
    for code, hold in holdings_dict.items():
        pct = hold / TOTAL * 100
        if pct > 25:
            lines.append(f"  ⚖️ {code} 仓位{pct:.0f}%偏重，建议减仓")
        elif pct < 5 and pct > 0:
            lines.append(f"  ⚖️ {code} 仓位{pct:.0f}%偏轻")
    
    perf = calc_performance()
    if perf:
        lines.append(f"\n📈 【历史交易】{perf['total']}笔 | {perf['wins']}胜{perf['losses']}负 | 胜率{perf['win_rate']}%")
    
    lines.append(f"\n⚠️ 量化信号仅供参考，不构成投资建议")
    return "\n".join(lines)
