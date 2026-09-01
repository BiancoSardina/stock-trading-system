#!/usr/bin/env python3
"""
ETF 周度再平衡系统 — 每周一执行
核心逻辑：
1. 计算每只ETF的相对强弱排名（1周/2周/4周涨幅 vs 沪深300）
2. 检查周线趋势方向
3. 根据排名和趋势给出再平衡建议
4. 只在偏离度超过阈值时才操作（减少无效交易）
"""
import json, urllib.request, sys, os
from datetime import datetime

ETF_TOTAL = 50000

# ETF配置
ETFS = [
    {"code": "510300", "name": "沪深300ETF", "target": 0.25, "hold": 5000},
    {"code": "588000", "name": "科创50ETF",  "target": 0.25, "hold": 5000},
    {"code": "159732", "name": "消费电子ETF", "target": 0.25, "hold": 5000},
    {"code": "518880", "name": "黄金ETF",    "target": 0.25, "hold": 5000},
]

EXTRA_ETFS = [
    {"code": "159516", "name": "半导体设备ETF", "hold": 11500},
    {"code": "515880", "name": "通信ETF",      "hold": 11500},
]

def sina_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn"
    })
    return urllib.request.urlopen(req, timeout=10).read()

def get_prefix(code):
    return "sh" if code.startswith(("51", "58", "60")) else "sz"

def get_kline(code, days=120):
    pref = get_prefix(code)
    url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={pref}{code}&scale=240&ma=5&datalen={days}"
    return json.loads(sina_get(url).decode("gbk"))

def get_rt(code):
    pref = get_prefix(code)
    data = sina_get(f"https://hq.sinajs.cn/list={pref}{code}").decode("gbk")
    parts = data.split(",")
    if len(parts) >= 10:
        return {"cur": float(parts[3]), "prev": float(parts[2]),
                "high": float(parts[4]), "low": float(parts[5])}
    return None

def get_indices():
    data = sina_get("https://hq.sinajs.cn/list=sh000001,sz399006,sh000688").decode("gbk")
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

def calc_weekly_ret(closes):
    """计算1周、2周、4周收益率"""
    ret = {}
    if len(closes) >= 5:
        ret["1周"] = round((closes[-1] - closes[-5]) / closes[-5] * 100, 2)
    if len(closes) >= 10:
        ret["2周"] = round((closes[-1] - closes[-10]) / closes[-10] * 100, 2)
    if len(closes) >= 20:
        ret["4周"] = round((closes[-1] - closes[-20]) / closes[-20] * 100, 2)
    return ret

def main():
    today = datetime.now().strftime("%Y-%m-%d")
    week_num = datetime.now().isocalendar()[1]
    
    print(f"╔════════════════════════════════╗")
    print(f"║  📊 ETF周度再平衡系统          ║")
    print(f"║  {today}  第{week_num}周                ║")
    print(f"║  ETF资金:{ETF_TOTAL}元                    ║")
    print(f"╚════════════════════════════════╝")
    
    # 大盘
    print("\n📊 【上周大盘】")
    indices = get_indices()
    for name, d in indices.items():
        e = "🟢" if d["chg"]>0 else "🔴"
        print(f"  {e} {name}: {d['price']} ({d['chg']:+.2f}%)")
    
    # 获取基准（沪深300作为benchmark）
    benchmark_kline = get_kline("510300", 120)
    bench_closes = [float(k["close"]) for k in benchmark_kline] if benchmark_kline else []
    bench_ret = calc_weekly_ret(bench_closes)
    
    print(f"\n📈 【相对强弱排名】")
    print(f"  {'ETF':<16} {'1周%':>7} {'2周%':>7} {'4周%':>7} {'vs大盘':>8} {'趋势':>6}")
    print("  " + "-" * 55)
    
    all_etfs = ETFS + EXTRA_ETFS
    rankings = []
    
    for etf in all_etfs:
        c = etf["code"]
        n = etf["name"]
        hold = etf.get("hold", etf.get("target", 0) * ETF_TOTAL)
        
        try:
            kline = get_kline(c, 120)
            closes = [float(k["close"]) for k in kline]
            highs = [float(k["high"]) for k in kline]
            lows = [float(k["low"]) for k in kline]
        except:
            print(f"  ❌ {n} 数据获取失败")
            continue
        
        ret = calc_weekly_ret(closes)
        
        # 周线趋势
        wma5 = sum(closes[-25:])/25 if len(closes) >= 25 else None
        wma10 = sum(closes[-50:])/50 if len(closes) >= 50 else None
        weekly_trend = "📈" if (wma5 and wma10 and closes[-1] > wma5 > wma10) else \
                       "📉" if (wma5 and wma10 and closes[-1] < wma5 < wma10) else "➡️"
        
        # 相对强弱（vs 沪深300 1周）
        rs = ret.get("1周", 0) - bench_ret.get("1周", 0)
        rs_str = f"{rs:+.1f}%"
        
        print(f"  {n:<12} {ret.get('1周','N/A'):>7} {ret.get('2周','N/A'):>7} {ret.get('4周','N/A'):>7} {rs_str:>8} {weekly_trend:>4}")
        
        rankings.append({
            "code": c, "name": n, "hold": int(hold),
            "ret1w": ret.get("1周", -999), "ret2w": ret.get("2周", -999),
            "rs_vs_benchmark": rs, "trend": weekly_trend,
            "closes": closes, "highs": highs, "lows": lows,
        })
    
    # ===== 再平衡建议 =====
    print(f"\n{'='*55}")
    print(f"🔄 【再平衡建议】")
    
    # 排名：按1周相对强弱排序
    rankings.sort(key=lambda x: x["rs_vs_benchmark"], reverse=True)
    
    total_hold = sum(r["hold"] for r in rankings)
    available = ETF_TOTAL - total_hold
    
    # 主ETF的目标仓位
    target_per_etf = ETF_TOTAL * 0.25  # 等权各25%
    
    rebalance_trades = []
    
    for r in rankings:
        code = r["code"]
        name = r["name"]
        hold = r["hold"]
        rs = r["rs_vs_benchmark"]
        trend = r["trend"]
        
        # 判断是否需要调整
        # 规则1: 周线趋势向下且仓位>10% → 减仓
        # 规则2: 相对强弱<-3%且仓位>10% → 减仓  
        # 规则3: 主ETF偏离目标超过±7% → 再平衡
        
        deviation = hold - target_per_etf
        
        action = "⚪ 不动"
        amount = 0
        reason = ""
        
        # 检查是否在EXTRA（附加持仓不做等权再平衡，但做风控）
        is_extra = any(e["code"] == code for e in EXTRA_ETFS)
        
        if is_extra:
            # 附加持仓：趋势向下就减，其他情况不动
            if "📉" in trend and hold > 5000:
                action = "🔴 减仓"
                amount = int(hold * 0.3)  # 减30%
                reason = "周线偏空，控制风险"
            else:
                action = "⚪ 持有"
                amount = 0
                reason = "附加持仓，保持观察"
        else:
            # 主组合：再平衡
            if "📉" in trend and rs < -3:
                action = "🔴 减仓"
                amount = int(hold * 0.4)
                reason = f"周线偏空+跑输大盘{rs:.1f}%，降低仓位"
            elif deviation > ETF_TOTAL * 0.07:
                action = "🔴 减仓"
                amount = int(deviation - ETF_TOTAL * 0.03)
                reason = f"仓位{hold/ETF_TOTAL*100:.0f}%偏重，回归25%"
            elif deviation < -ETF_TOTAL * 0.07 and available > 1000:
                action = "🟢 加仓"
                amount = min(int(-deviation - ETF_TOTAL * 0.03), available)
                reason = f"仓位{hold/ETF_TOTAL*100:.0f}%偏轻，回归25%"
            elif "📈" in trend and rs > 2 and hold < ETF_TOTAL * 0.30:
                action = "🟢 加仓"
                amount = min(int(ETF_TOTAL * 0.03), available)
                reason = f"周线偏多+跑赢大盘{rs:.1f}%，适当加仓"
            else:
                action = "⚪ 持有"
                amount = 0
                reason = "仓位合理，无需操作"
        
        rebalance_trades.append((code, name, action, amount, reason, hold))
        
        if "减仓" in action:
            available += amount
        elif "加仓" in action:
            available -= amount
    
    # 输出再平衡表
    print(f"  {'ETF':<16} {'现持仓':>8} {'操作':>12} {'金额':>8} {'理由'}")
    print("  " + "-" * 60)
    for code, name, action, amount, reason, hold in rebalance_trades:
        amt_str = f"{amount}元" if amount > 0 else ""
        print(f"  {name:<12} {hold:>6}元 {action:>8} {amt_str:>8} {reason}")
    
    # ===== 总结 =====
    print(f"\n💡 【本周操作要点】")
    print(f"  总持仓:{total_hold}元  现金:{ETF_TOTAL - total_hold}元")
    print(f"  本周建议操作次数:{sum(1 for _,_,a,_,_,_ in rebalance_trades if '不动' not in a and '持有' not in a)}笔")
    print(f"  ✅ 原则：周线偏多的ETF多配，偏空的少配")
    print(f"  ✅ 再平衡频率：每周一检查一次，中间不操作")
    
    print(f"\n⚠️ 周度再平衡建议仅供参考")

if __name__ == "__main__":
    main()
