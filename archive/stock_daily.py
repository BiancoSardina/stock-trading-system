#!/usr/bin/env python3
"""
个股量化交易系统 — 每日14:45尾盘信号
读取 stock_config.py 中的个股列表，用量化引擎分析
"""
import json, urllib.request, sys, os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quant_engine

# 加载个股配置
try:
    from stock_config import STOCKS, TOTAL
except:
    STOCKS = []
    TOTAL = 50000

def sina_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn"
    })
    return urllib.request.urlopen(req, timeout=10).read()

def get_prefix(code):
    return "sh" if code.startswith(("51", "58", "60", "68")) else "sz"

def get_kline(code, days=120):
    pref = get_prefix(code)
    url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={pref}{code}&scale=240&ma=5&datalen={days}"
    data = sina_get(url).decode("gbk")
    return json.loads(data)

def get_rt(code):
    pref = get_prefix(code)
    data = sina_get(f"https://hq.sinajs.cn/list={pref}{code}").decode("gbk")
    parts = data.split(",")
    if len(parts) >= 10:
        return {
            "open": float(parts[1]), "prev": float(parts[2]), "cur": float(parts[3]),
            "high": float(parts[4]), "low": float(parts[5]), "vol": int(parts[8])
        }
    return None

def main():
    if not STOCKS:
        print("⚠️ 未配置个股。请在 stock_config.py 中填写你的股票。")
        print("   格式: (\"600519\", \"贵州茅台\", \"sh\", 10000),")
        return
    
    today = datetime.now().strftime("%Y-%m-%d")
    
    print(f"╔════════════════════════════════╗")
    print(f"║  📈 个股量化交易系统            ║")
    print(f"║  {today} 尾盘信号                 ║")
    print(f"║  总资金:{TOTAL}元  持仓:{len(STOCKS)}只  ║")
    print(f"╚════════════════════════════════╝")
    
    # 获取基准（沪深300）
    bench_rt = get_rt("510300")
    benchmark_chg = None
    if bench_rt:
        benchmark_chg = round((bench_rt["cur"]-bench_rt["prev"])/bench_rt["prev"]*100, 2)
    
    # 大盘
    print(f"\n📊 【大盘】沪深300: {bench_rt['cur'] if bench_rt else 'N/A'} ({benchmark_chg:+.2f}%)" if benchmark_chg else "")
    
    quant_data = []
    holdings = {}
    
    for code, name, exchange, hold in STOCKS:
        rt = get_rt(code)
        if not rt:
            print(f"\n❌ {name}({code}) 数据获取失败")
            continue
        
        try:
            kline = get_kline(code, 120)
            closes = [float(k["close"]) for k in kline]
            highs = [float(k["high"]) for k in kline]
            lows = [float(k["low"]) for k in kline]
            vols = [int(k["volume"]) for k in kline]
        except:
            print(f"\n❌ {name}({code}) K线数据失败")
            continue
        
        holdings[code] = hold
        chg = round((rt["cur"]-rt["prev"])/rt["prev"]*100, 2)
        
        quant_data.append({
            "code": code, "name": name,
            "cur": rt["cur"], "chg": chg,
            "closes": closes, "highs": highs, "lows": lows, "vols": vols,
            "benchmark_chg": benchmark_chg,
        })
    
    if quant_data:
        print(f"\n{quant_engine.generate_quant_report(quant_data, holdings)}")
    
    print(f"\n💡 【操作提醒】")
    print(f"  个股按量化信号操作，14:45是尾盘最后窗口")
    print(f"  止损严格按系统建议执行，不要扛单")
    print(f"\n⚠️ 量化信号仅供参考，不构成投资建议")

if __name__ == "__main__":
    main()
