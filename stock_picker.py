#!/usr/bin/env python3
"""
周度优质个股选股系统 v1.0
每周日执行：全市场扫描 → 短期(1-2天)2-3只 + 中期(半月-1月)2-3只
数据源：新浪财经API
规则基于 stock-short-term-trading 强势股框架 + 威科夫择时 + 趋势三要件
"""
import json, urllib.request, sys, os, time
from datetime import datetime

def sina_get(url, gbk=True):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
    raw = urllib.request.urlopen(req, timeout=15).read()
    return raw.decode("gbk") if gbk else raw.decode("utf-8", "ignore")

def get_prefix(code):
    return "sh" if code.startswith(("5", "6", "9")) else "sz"

def get_rt(code):
    pref = get_prefix(code)
    try:
        data = sina_get(f"https://hq.sinajs.cn/list={pref}{code}")
        parts = data.split(",")
        if len(parts) >= 32:
            return {
                "name": parts[0].split('"')[-1],
                "open": float(parts[1]), "prev": float(parts[2]), "cur": float(parts[3]),
                "high": float(parts[4]), "low": float(parts[5]), "vol": int(parts[8]),
                "amount": float(parts[9]), "date": parts[30],
            }
    except Exception:
        pass
    return None

def get_kline(code, days=130):
    pref = get_prefix(code)
    url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={pref}{code}&scale=240&ma=5&datalen={days}"
    try:
        return json.loads(sina_get(url))
    except Exception:
        return None

def get_market_list(page, num=100):
    """新浪全市场A股列表接口，分页拉取"""
    url = (f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
           f"?page={page}&num={num}&sort=changepercent&asc=0&node=hs_a&symbol=&_s_r_a=page")
    try:
        data = sina_get(url)
        return json.loads(data)
    except Exception:
        return []

def calc_rsi(closes, n=14):
    if len(closes) < n+1: return None
    g = l = 0
    for i in range(-n, 0):
        d = closes[i] - closes[i-1]
        if d > 0: g += d
        else: l += abs(d)
    ag, al = g/n, l/n
    return round(100 - 100/(1+ag/al), 1) if al != 0 else 100

def calc_ma(prices, n):
    if not prices or len(prices) < n: return None
    return sum(prices[-n:])/n

def analyze_stock(code, name):
    """对单只股票做完整技术分析，返回评分和各维度"""
    rt = get_rt(code)
    if not rt: return None
    kline = get_kline(code)
    if not kline or len(kline) < 70: return None
    closes = [float(k["close"]) for k in kline]
    highs = [float(k["high"]) for k in kline]
    lows = [float(k["low"]) for k in kline]
    vols = [int(k["volume"]) for k in kline]
    cur = rt["cur"]; prev = rt["prev"]
    chg = round((cur-prev)/prev*100, 2)
    amount = rt["amount"]

    ma5 = calc_ma(closes, 5); ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20); ma60 = calc_ma(closes, 60)
    ma60_prev = calc_ma(closes[:-5], 60) if len(closes) >= 65 else None
    ma60_slope = round((ma60-ma60_prev)/ma60_prev*100, 2) if ma60 and ma60_prev else None
    rsi14 = calc_rsi(closes); rsi6 = calc_rsi(closes, 6)
    avgv5 = sum(vols[-5:])/5
    vr = round(vols[-1]/avgv5, 2) if avgv5 > 0 else 1

    # 20日涨幅
    chg20 = round((cur/closes[-21]-1)*100, 2) if len(closes) >= 21 else None
    # 涨停基因：近20日内是否有涨幅>9.5%的K线
    limit_up = 0
    for i in range(-21, -1):
        if closes[i] and closes[i-1]:
            if (closes[i]/closes[i-1]-1)*100 > 9.5:
                limit_up += 1
    # 周线（用5日K聚合近似：周MA5=日MA25, 周MA10=日MA50）
    wma5 = calc_ma(closes, 25); wma10 = calc_ma(closes, 50)

    # ===== 评分维度 =====
    # 趋势三要件
    trend_ok = (ma60 and ma60_slope and ma60_slope > 0 and cur > ma60 and ma20 and ma20 > ma60)
    # 短期强度
    strong_short = chg >= 5 and vr >= 1.5
    # RS相对强度（相对上证，简单用20日涨幅>0近似，实际任务里AI会对比大盘）
    rs_ok = chg20 is not None and chg20 > 0
    # 中期结构
    mid_ok = trend_ok and wma5 and wma10 and wma5 > wma10
    # RSI健康区
    rsi_ok = rsi6 is not None and 55 <= rsi6 <= 85

    return {
        "code": code, "name": name, "cur": cur, "chg": chg, "amount": amount,
        "chg20": chg20, "vr": vr, "rsi6": rsi6, "rsi14": rsi14,
        "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "ma60_slope": ma60_slope, "wma5": wma5, "wma10": wma10,
        "trend_ok": trend_ok, "mid_ok": mid_ok, "rs_ok": rs_ok,
        "limit_up": limit_up, "strong_short": strong_short, "rsi_ok": rsi_ok,
    }

def main():
    # 参数：--short N --mid N（默认 2/1，合计 2-3 只，高风险短线宁缺毋滥）
    short_n, mid_n = 2, 1
    argv = sys.argv[1:]
    if "--short" in argv:
        short_n = int(argv[argv.index("--short") + 1])
    if "--mid" in argv:
        mid_n = int(argv[argv.index("--mid") + 1])

    print("╔═══════════════════════════════════════╗")
    print("║  🏆 周度优质个股选股报告               ║")
    print(f"║  {datetime.now().strftime('%Y-%m-%d %H:%M')}                  ║")
    print("╚═══════════════════════════════════════╝")

    # 1. 拉全市场列表（按涨幅排序，分页拉足够覆盖强势股）
    print("\n📡 扫描全市场...")
    candidates = []
    seen = set()
    for page in range(1, 16):  # 拉1500只（按涨幅排序，覆盖当日强势股）
        stocks = get_market_list(page)
        if not stocks: break
        for s in stocks:
            code = s.get("code", "")
            name = s.get("name", "")
            if not code or code in seen: continue
            seen.add(code)
            # 粗筛：排除ST/退市/异常
            if "ST" in name.upper() or "退" in name: continue
            # 只保留沪深主板：创业板(300/301)需10万资产门槛、科创板(688/689)需50万、北交所(8/4/920)需50万——用户资金不足未开通，一律排除
            if not code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")): continue
            try:
                amount = float(s.get("amount", 0))
                chg = float(s.get("changepercent", 0))
                cur = float(s.get("trade", 0))
            except Exception:
                continue
            if amount < 80_000_000: continue  # 成交额<8000万排除（流动性）
            if cur < 3 or cur > 100: continue  # 低价垃圾/超高价排除（散户范围）
            candidates.append({"code": code, "name": name, "chg": chg, "amount": amount})
        time.sleep(0.15)
    print(f"  粗筛通过: {len(candidates)}只 (成交额>8000万, 3-100元)")

    # 2. 对候选做完整技术分析（前80只，按涨幅）
    candidates.sort(key=lambda x: -x["chg"])
    results = []
    for c in candidates[:80]:
        r = analyze_stock(c["code"], c["name"])
        if r:
            results.append(r)
        time.sleep(0.1)
    print(f"  技术分析完成: {len(results)}只")

    # 3. 分类打分
    short_list = []  # 短期：强动量+涨停基因
    mid_list = []    # 中期：趋势三要件+周线
    for r in results:
        # 短期评分
        s_score = 0
        if r["chg"] >= 7: s_score += 3
        elif r["chg"] >= 5: s_score += 2
        elif r["chg"] >= 3: s_score += 1
        if r["vr"] and r["vr"] >= 1.5: s_score += 2
        if r["limit_up"] and r["limit_up"] >= 1: s_score += 2
        if r["rsi_ok"]: s_score += 1
        if r["rs_ok"]: s_score += 1
        if r["chg20"] is not None and r["chg20"] < 60: s_score += 1  # 非爆炒
        r["s_score"] = s_score

        # 中期评分
        m_score = 0
        if r["mid_ok"]: m_score += 4
        elif r["trend_ok"]: m_score += 2
        if r["ma60_slope"] and r["ma60_slope"] > 0: m_score += 2
        if r["rs_ok"] and r["chg20"] and r["chg20"] > 5: m_score += 2
        if r["rsi14"] and 50 <= r["rsi14"] <= 70: m_score += 1
        if r["chg20"] is not None and r["chg20"] < 60: m_score += 1
        r["m_score"] = m_score

        if s_score >= 5 and r["chg"] > 0:
            short_list.append(r)
        if m_score >= 6:
            mid_list.append(r)

    short_list.sort(key=lambda x: -x["s_score"])
    mid_list.sort(key=lambda x: -x["m_score"])

    # 4. 输出
    print("\n" + "="*55)
    print(f"🔥 【短期候选 1-2天】（强动量+涨停基因，仅{short_n}只）")
    print("="*55)
    for r in short_list[:short_n]:
        k5 = get_kline(r["code"])[-5:]
        sup = round(min(float(k["low"]) for k in k5), 2)
        res = round(max(float(k["high"]) for k in k5), 2)
        stop = round(r["ma5"]*0.93, 2) if r["ma5"] else round(sup*0.95, 2)  # 5日线下方7%止损
        print(f"\n🟢 【{r['name']}({r['code']})】短期评分:{r['s_score']}")
        print(f"  现价:{r['cur']} 今日:{r['chg']:+.2f}% 量比:{r['vr']} RSI6:{r['rsi6']}")
        print(f"  20日涨幅:{r['chg20']:+.1f}% 涨停基因:{r['limit_up']}次 成交额:{r['amount']/1e8:.1f}亿")
        print(f"  🎯 买入:回踩{r['ma5']:.2f}附近(5日线)企稳买  |  卖出:冲高+5~8%减 | 止损:{stop}")

    print("\n" + "="*55)
    print(f"📈 【中期候选 半月-1月】（趋势三要件+周线多头，仅{mid_n}只）")
    print("="*55)
    for r in mid_list[:mid_n]:
        k20 = get_kline(r["code"])[-20:]
        sup = round(min(float(k["low"]) for k in k20), 2)
        res = round(max(float(k["high"]) for k in k20), 2)
        stop = round(r["ma20"]*0.92, 2) if r["ma20"] else round(sup*0.9, 2)
        print(f"\n🟢 【{r['name']}({r['code']})】中期评分:{r['m_score']}")
        print(f"  现价:{r['cur']} 今日:{r['chg']:+.2f}% 20日涨幅:{r['chg20']:+.1f}%")
        print(f"  MA20:{r['ma20']:.2f} MA60:{r['ma60']:.2f}(斜率{r['ma60_slope']:+.2f}%) 周线{'多头' if r['wma5'] and r['wma10'] and r['wma5']>r['wma10'] else '待确认'}")
        print(f"  🎯 买入:回踩MA10/MA20({r['ma10']:.2f}/{r['ma20']:.2f})缩量企稳买 | 卖出:前高{res}附近 | 止损:破MA20({stop:.2f})")

    # 5. 大盘环境
    print("\n" + "="*55)
    print("📊 【大盘环境】")
    try:
        # 上证指数单独拉（sh000001）
        data = sina_get("https://hq.sinajs.cn/list=sh000001")
        parts = data.split(",")
        if len(parts) >= 4:
            cur = float(parts[3]); prev = float(parts[2])
            print(f"  上证指数: {cur} ({'+' if cur>=prev else ''}{(cur-prev)/prev*100:.2f}%)")
    except Exception as e:
        print(f"  大盘数据获取失败: {e}")

    print("\n⚠️ 选股为量化初筛，AI深度分析将结合金十消息面/基本面二次确认")
    print("⚠️ 仅供参考，投资有风险！")

if __name__ == "__main__":
    main()
