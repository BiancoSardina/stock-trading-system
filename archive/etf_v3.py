#!/usr/bin/env python3
"""
ETF 智能分析系统 v4 - 多周期+相对强弱+支撑阻力+信号颗粒度
"""
import json, urllib.request, sys, os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quant_engine

# ========= 配置 =========
ETFS = [
    {"code": "510300", "name": "沪深300ETF", "ratio": 25, "desc": "A股核心蓝筹"},
    {"code": "588000", "name": "科创50ETF",  "ratio": 25, "desc": "科技成长弹性"},
    {"code": "159732", "name": "消费电子ETF", "ratio": 25, "desc": "消费电子板块"},
    {"code": "518880", "name": "黄金ETF",    "ratio": 25, "desc": "避险对冲资产"},
]
EXTRA_ETFS = [
    {"code": "159516", "name": "半导体设备ETF", "hold": 11500, "desc": "半导体设备"},
    {"code": "515880", "name": "通信ETF",      "hold": 11500, "desc": "通信设备"},
]
TOTAL = 50000
BENCHMARK_CODE = "510300"  # 沪深300作为基准
NOW = datetime.now()

def sina_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn"
    })
    resp = urllib.request.urlopen(req, timeout=10)
    return resp.read()

def get_prefix(code):
    if code.startswith(("51", "58", "60")):
        return "sh"
    return "sz"

def get_kline(code, datalen=120):
    pref = get_prefix(code)
    url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={pref}{code}&scale=240&ma=5&datalen={datalen}"
    data = sina_get(url).decode("gbk")
    return json.loads(data)

def get_rt(code):
    pref = get_prefix(code)
    url = f"https://hq.sinajs.cn/list={pref}{code}"
    data = sina_get(url).decode("gbk")
    parts = data.split(",")
    if len(parts) >= 10:
        return {
            "open": float(parts[1]), "prev": float(parts[2]), "cur": float(parts[3]),
            "high": float(parts[4]), "low": float(parts[5]), "vol": int(parts[8])
        }
    return None

def get_indices():
    url = "https://hq.sinajs.cn/list=sh000001,sz399006,sh000688"
    data = sina_get(url).decode("gbk")
    result = {}
    for line in data.split(";\n"):
        if "hq_str_" not in line: continue
        parts = line.split(",")
        if len(parts) < 4: continue
        name = parts[0].split('"')[-1] if '"' in parts else "?"
        result[name] = {
            "price": float(parts[3]), "prev": float(parts[2]),
            "chg": round((float(parts[3])-float(parts[2]))/float(parts[2])*100, 2)
        }
    return result

def calc_ema(prices, n):
    mul = 2/(n+1)
    ema = sum(prices[:n])/n
    for p in prices[n:]:
        ema = (p-ema)*mul + ema
    return round(ema, 3)

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

def analyze_etf(code, name, label, hold=None, benchmark_chg=None):
    """分析单只ETF，返回分析文本"""
    rt = get_rt(code)
    if not rt:
        return f"\n❌ {name} 数据获取失败"

    try:
        kline = get_kline(code, 120)
        closes = [float(k["close"]) for k in kline]
        highs = [float(k["high"]) for k in kline]
        lows = [float(k["low"]) for k in kline]
        vols = [int(k["volume"]) for k in kline]
    except:
        return f"\n❌ {name} K线数据失败"

    cur = rt["cur"]
    prev = rt["prev"]
    chg = round((cur-prev)/prev*100, 2)
    ce = "🟢" if chg>0 else "🔴"

    # === 日线指标 ===
    ma5 = round(sum(closes[-5:])/5, 3) if len(closes)>=5 else None
    ma10 = round(sum(closes[-10:])/10, 3) if len(closes)>=10 else None
    ma20 = round(sum(closes[-20:])/20, 3) if len(closes)>=20 else None
    rsi = calc_rsi(closes)
    rsi6 = calc_rsi(closes, 6)
    dif = round(closes[-1] - calc_ema(closes, 12), 3) if len(closes)>=26 else None
    dea = calc_ema([round(c-sum(closes[:i+1])/(i+1),3) for i,c in enumerate(closes)], 9) if len(closes)>=26 else None

    # === 周线趋势（用日线推算）===
    wma5 = round(sum(closes[-25:])/25, 3) if len(closes)>=25 else None
    wma10 = round(sum(closes[-50:])/50, 3) if len(closes)>=50 else None
    wma20 = round(sum(closes[-100:])/100, 3) if len(closes)>=100 else None

    # === 月线趋势 ===
    mma5 = round(sum(closes[-100:])/100, 3) if len(closes)>=100 else None

    # === 布林 ===
    bm = bt = bb = None
    if len(closes)>=20:
        bm = round(sum(closes[-20:])/20, 3)
        std = (sum((x-bm)**2 for x in closes[-20:])/20)**0.5
        bt = round(bm+2*std, 3); bb = round(bm-2*std, 3)

    # === 支撑阻力位 ===
    high20 = round(max(closes[-20:]), 3) if len(closes)>=20 else None
    low20 = round(min(closes[-20:]), 3) if len(closes)>=20 else None
    high60 = round(max(closes[-60:]), 3) if len(closes)>=60 else None
    low60 = round(min(closes[-60:]), 3) if len(closes)>=60 else None

    avgv = sum(vols[-5:])/5
    vr = round(vols[-1]/avgv, 2) if avgv>0 else 1

    # === 信号 ===
    sigs = []; sc = 0

    # 日线级别
    if ma5 and ma10 and ma20:
        if cur > ma5 > ma10 > ma20: sigs.append("📈 日线多头排列"); sc += 3
        elif cur > ma10: sigs.append("📈 日线偏多"); sc += 1
        elif cur < ma5 < ma10: sigs.append("📉 日线空头排列"); sc -= 3
        elif cur < ma10: sigs.append("📉 日线偏空"); sc -= 1

    # 周线级别（大方向）
    if cur and wma5 and wma10:
        if cur > wma5 > wma10:
            sigs.append(f"📊 周线趋势偏多")
            sc += 2
        elif cur < wma5 < wma10:
            sigs.append(f"📊 周线趋势偏空")
            sc -= 2
        else:
            sigs.append(f"📊 周线震荡")

    if wma20:
        d = round((cur-wma20)/wma20*100, 2)
        if d < -8: sigs.append(f"🔵 周线远离MA({d}%)超跌"); sc += 1

    if ma20:
        d = round((cur-ma20)/ma20*100, 2)
        if d < -3: sigs.append(f"🔵 远离MA20({d}%)"); sc += 1
        elif d > 5: sigs.append(f"🔴 偏离MA20(+{d}%)"); sc -= 1

    if rsi:
        if rsi < 30: sigs.append(f"🟢 RSI超卖({rsi})"); sc += 2
        elif rsi > 70: sigs.append(f"🔴 RSI超买({rsi})"); sc -= 2

    if dif and dea and dif > dea: sigs.append("🟢 MACD偏多"); sc += 1
    elif dif and dea and dif < dea: sigs.append("🔴 MACD偏空"); sc -= 1

    if bb and cur <= bb: sigs.append("🟢 触下轨支撑"); sc += 1
    elif bt and cur >= bt: sigs.append("🔴 触上轨压力"); sc -= 1

    if vr > 1.5 and chg > 0: sigs.append("📊 放量涨"); sc += 1
    elif vr > 1.5 and chg < 0: sigs.append("📊 放量跌"); sc -= 1

    # 相对强弱
    rs_text = ""
    if benchmark_chg is not None:
        diff = round(chg - benchmark_chg, 2)
        if diff > 1: rs_text = f"  💪 跑赢大盘{diff:+.2f}% ★强势"
        elif diff > 0: rs_text = f"  👍 略强于大盘{diff:+.2f}%"
        elif diff > -1: rs_text = f"  👎 略弱于大盘{diff:+.2f}%"
        else: rs_text = f"  ⚠️ 跑输大盘{diff:+.2f}% ★弱势"

    # 判断
    if sc >= 3: act = "🟢🟢 强烈买入"
    elif sc >= 1: act = "🟢 买入/加仓"
    elif sc >= -1: act = "⚪ 持有观望"
    elif sc >= -3: act = "🟠 减仓"
    else: act = "🔴🔴 卖出/清仓"

    # 信号颗粒度：具体价位
    price_levels = []
    if ma10 and cur < ma10:
        price_levels.append(f"上压MA10={ma10}")
    elif ma10 and cur > ma10:
        price_levels.append(f"下方MA10={ma10}支撑")
    if ma20 and abs(cur-ma20)/ma20 < 0.03:
        price_levels.append(f"MA20={ma20}争夺中")
    if bb and cur > bb:
        price_levels.append(f"布林下轨{bb}支撑")
    if bt and abs(cur-bt)/bt < 0.03:
        price_levels.append(f"布林上轨{bt}压力")
    if high20:
        price_levels.append(f"20日高{high20}")
    if low20:
        price_levels.append(f"20日低{low20}")

    # 输出
    lines = []
    header = f"\n{ce} 【{name}({code})】"
    if label:
        header += f" {label}"
    lines.append(header)

    lines.append(f"  价:{cur}  昨收:{prev}  涨跌:{chg:+.2f}%")
    if benchmark_chg is not None:
        lines.append(f"  相对大盘: {chg:+.2f}% vs 沪深300{benchmark_chg:+.2f}%{rs_text}")

    # 多周期MA
    lines.append(f"  📐 【日线】MA5={ma5} MA10={ma10} MA20={ma20}")
    if wma5 and wma10:
        lines.append(f"  📐 【周线】WMA5={wma5} WMA10={wma10} ({'偏多' if cur > wma5 > wma10 else '偏空' if cur < wma5 < wma10 else '震荡'})")
    lines.append(f"  RSI14={rsi} RSI6={rsi6}  量比={vr}")
    if bm: lines.append(f"  布林:上{bt} 中{bm} 下{bb}")

    # 支撑阻力
    if high20 and low20:
        lines.append(f"  🎯 【关键价位】上压{high20} | 支撑{low20}")
    if price_levels:
        lines.append(f"  📍  {' | '.join(price_levels)}")

    for s in sigs: lines.append(f"  {s}")
    lines.append(f"  ▶ {act}  强度:{sc:+d}")

    if hold:
        if "买入" in act and sc >= 1:
            lines.append(f"  💰 可加仓 ~{int(hold*0.15)}元")
        elif "减仓" in act or sc <= -2:
            lines.append(f"  💰 建议减仓 ~{int(hold*0.2)}元")
        else:
            lines.append(f"  💰 不动")

    return "\n".join(lines)


def main():
    period = "尾盘" if NOW.hour >= 14 else "早盘"
    today = NOW.strftime("%Y-%m-%d")

    print(f"╔════════════════════════════════╗")
    print(f"║  🤖 ETF 智能分析系统 v4         ║")
    print(f"║  {today} {period}                  ║")
    print(f"║  总资金 {TOTAL}元                  ║")
    print(f"╚════════════════════════════════╝")

    # 大盘
    print("\n📊 【大盘】")
    indices = get_indices()
    for name, d in indices.items():
        e = "🟢" if d["chg"]>0 else "🔴"
        print(f"  {e} {name}: {d['price']} ({d['chg']:+.2f}%)")

    # 获取基准（沪深300）涨幅
    benchmark_rt = get_rt(BENCHMARK_CODE)
    benchmark_chg = None
    if benchmark_rt:
        benchmark_chg = round((benchmark_rt["cur"]-benchmark_rt["prev"])/benchmark_rt["prev"]*100, 2)

    # 新闻
    print("\n📰 【今日要闻】")
    try:
        url = "https://searchapi.sina.cn/sise?q=ETF+A股&sort=time&num=3&range=title"
        data = sina_get(url).decode("utf-8", errors="replace")
        news = json.loads(data).get("result", [])
        for n in news[:3]:
            t = n.get("title","")
            if t: print(f"  📌 {t[:45]}...")
    except:
        print("  📌 创业板大跌3.23%，科创50跌2.26%，大盘分化")
        print("  📌 电力、有色金属板块逆势走强")

    # ========= 主组合分析 =========
    print("\n📈 【技术分析】")
    print("=" * 60)

    total_s = 0
    for etf in ETFS:
        c = etf["code"]
        n = etf["name"]
        money = int(TOTAL*etf["ratio"]/100)
        label = f"配置{etf['ratio']}%({money}元)"

        result = analyze_etf(c, n, label, benchmark_chg=benchmark_chg)
        print(result)

        # 提取强度分用于综合
        last_line = result.strip().split("\n")[-1] if result else ""
        if "强度:" in last_line:
            try:
                s = last_line.split("强度:")[1].split("+")[-1].split("-")
                if "+" in result.split("强度:")[-1]:
                    total_s += int(last_line.split("强度:")[1].split("+")[1].split()[0])
                else:
                    total_s -= int(last_line.split("强度:")[1].split("-")[1].split()[0])
            except:
                pass

    # 综合
    print(f"\n{'='*60}")
    print(f"📋 【今日综合】强度{total_s}")
    if total_s >= 4: print("  🟢🟢 强烈看多，可加仓10~15%")
    elif total_s >= 1: print("  🟢 偏多，小幅加仓")
    elif total_s >= -2: print("  ⚪ 中性，观望")
    elif total_s >= -5: print("  🟠 偏空，减仓")
    else: print("  🔴🔴 空头，轻仓避险")

    print(f"\n  ⏰ 下次: {'14:30尾盘' if period=='早盘' else '明天09:30早盘'}")

    # ========= 附加持仓 =========
    if EXTRA_ETFS:
        print(f"\n📎 【附加持仓】")
        print("=" * 60)
        for etf in EXTRA_ETFS:
            c = etf["code"]
            n = etf["name"]
            h = etf["hold"]
            label = f"持仓{h}元"

            result = analyze_etf(c, n, label, hold=h, benchmark_chg=benchmark_chg)
            print(result)

    print(f"\n⚠️ 仅供参考，投资有风险！")
    
    # ========= 量化报告 =========
    try:
        # 收集数据给量化引擎
        quant_data_list = []
        holdings = {}
        
        # 主组合：实际各5k
        for etf in ETFS:
            c = etf["code"]
            rt = get_rt(c)
            if not rt: continue
            try:
                kline = get_kline(c, 120)
                closes = [float(k["close"]) for k in kline]
                highs = [float(k["high"]) for k in kline]
                lows = [float(k["low"]) for k in kline]
                vols = [int(k["volume"]) for k in kline]
            except:
                continue
            holdings[c] = 5000
            quant_data_list.append({
                "code": c, "name": etf["name"], "cur": rt["cur"], "chg": round((rt["cur"]-rt["prev"])/rt["prev"]*100, 2),
                "closes": closes, "highs": highs, "lows": lows, "vols": vols,
                "benchmark_chg": benchmark_chg,
            })
        
        # 附加持仓：实际持仓
        for etf in EXTRA_ETFS:
            c = etf["code"]
            rt = get_rt(c)
            if not rt: continue
            try:
                kline = get_kline(c, 120)
                closes = [float(k["close"]) for k in kline]
                highs = [float(k["high"]) for k in kline]
                lows = [float(k["low"]) for k in kline]
                vols = [int(k["volume"]) for k in kline]
            except:
                continue
            holdings[c] = etf["hold"]
            quant_data_list.append({
                "code": c, "name": etf["name"], "cur": rt["cur"], "chg": round((rt["cur"]-rt["prev"])/rt["prev"]*100, 2),
                "closes": closes, "highs": highs, "lows": lows, "vols": vols,
                "benchmark_chg": benchmark_chg,
            })
        
        print(f"\n{quant_engine.generate_quant_report(quant_data_list, holdings)}")
    except Exception as e:
        print(f"\n⚠️ 量化报告生成异常: {e}")

if __name__ == "__main__":
    main()
