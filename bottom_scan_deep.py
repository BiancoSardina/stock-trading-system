#!/usr/bin/env python3
"""超跌池候选深度校验：趋势三要件 + RS强度 + ATR + 动态支撑/追涨位"""
import json, urllib.request, sys

def sina_get(url, gbk=True):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
    raw = urllib.request.urlopen(req, timeout=15).read()
    return raw.decode("gbk") if gbk else raw.decode("utf-8", "ignore")

def get_kline(symbol, days=130):
    url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=5&datalen={days}"
    try:
        return json.loads(sina_get(url))
    except Exception as e:
        return None

def calc_ma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n

def calc_atr(highs, lows, closes, n=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    if len(trs) < n:
        return None
    return sum(trs[-n:]) / n

def get_realtime(code):
    pref = "sh" if code.startswith(("5","6","9")) else "sz"
    url = f"https://hq.sinajs.cn/list={pref}{code}"
    try:
        raw = sina_get(url)
        parts = raw.split('"')[1].split(",")
        return {"cur": float(parts[3]), "high": float(parts[4]), "low": float(parts[5]),
                "prev_close": float(parts[2])}
    except Exception:
        return None

CANDIDATES = [
    ("601992", "金隅集团", "sh601992"),
    ("600959", "江苏有线", "sh600959"),
    ("002296", "辉煌科技", "sz002296"),
    ("600628", "新世界", "sh600628"),
    ("600383", "金地集团", "sh600383"),
    ("000860", "顺鑫农业", "sz000860"),
]

# 上证指数20日涨幅基准
idx = get_kline("sh000001", 30)
idx_chg20 = (float(idx[-1]["close"]) / float(idx[-21]["close"]) - 1) * 100 if idx and len(idx) >= 21 else None
print(f"上证指数20日涨幅: {idx_chg20:.2f}%" if idx_chg20 is not None else "上证指数数据缺失")

for code, name, symbol in CANDIDATES:
    kline = get_kline(symbol)
    rt = get_realtime(code)
    if not kline or not rt:
        print(f"\n{code} {name}: 数据获取失败")
        continue
    closes = [float(k["close"]) for k in kline]
    highs = [float(k["high"]) for k in kline]
    lows = [float(k["low"]) for k in kline]
    vols = [int(k["volume"]) for k in kline]
    cur = rt["cur"]
    # 并入当日实时高低点
    today_high = max(rt["high"], highs[-1])
    today_low = min(rt["low"], lows[-1]) if rt["low"] > 0 else lows[-1]

    ma5 = calc_ma(closes, 5); ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20); ma60 = calc_ma(closes, 60)
    ma60_5ago = calc_ma(closes[:-5], 60) if len(closes) >= 65 else None
    ma60_slope = (ma60 - ma60_5ago) / ma60_5ago * 100 if ma60 and ma60_5ago else None
    cond1 = ma60_slope > 0 if ma60_slope is not None else None
    cond2 = cur > ma60 if ma60 else None
    cond3 = ma20 > ma60 if ma20 and ma60 else None
    three_ok = (cond1 and cond2 and cond3) if all(c is not None for c in (cond1, cond2, cond3)) else None

    chg20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 else None
    rs = chg20 - idx_chg20 if chg20 is not None and idx_chg20 is not None else None
    atr = calc_atr(highs, lows, closes)

    # 动态支撑：近5日低点 vs MA10 取较高者
    low5 = min(lows[-5:])
    dyn_support = max(low5, ma10) if ma10 else low5
    # 距现价>7%则上移为现价×0.97
    if (cur - dyn_support) / cur > 0.07:
        dyn_support = cur * 0.97
    # 追涨位：近5日高点(含当日实时)×1.005
    high5 = max(highs[-5:] + [today_high])
    chase = high5 * 1.005
    # 破位放弃位：震荡下沿/近10日低
    low10 = min(lows[-10:] + [today_low])

    print(f"\n{'='*70}")
    print(f"{code} {name}  现价{cur:.2f}  今高{today_high:.2f} 今低{today_low:.2f}")
    print(f"MA5={ma5:.3f} MA10={ma10:.3f} MA20={ma20:.3f} MA60={ma60:.3f}" if ma5 else "")
    if ma60_slope is not None:
        print(f"MA60斜率: {ma60_slope:+.2f}%  三要件: {'✅全满足' if three_ok else '❌不满足'}"
              f"  [斜{'↑' if cond1 else '↓'} | 价{'上' if cond2 else '下'}MA60 | MA20{'上' if cond3 else '下'}MA60]")
    if rs is not None:
        print(f"RS强度: 20日跑赢大盘 {rs:+.2f}% (标的{chg20:+.2f}% vs 大盘{idx_chg20:+.2f}%)")
    if atr:
        print(f"ATR14: {atr:.3f} ({atr/cur*100:.2f}%/日)  止损参考≈{atr*2:.3f}")
    print(f"📌 动态支撑(回踩买): {dyn_support:.3f} (距现价 {(cur-dyn_support)/cur*100:+.1f}%)")
    print(f"📌 追涨位: {chase:.3f} (距现价 {(chase-cur)/cur*100:+.1f}%)")
    print(f"📌 破位弃: {low10:.3f} (距现价 {(low10-cur)/cur*100:+.1f}%)")
    print(f"📌 近5日均量/近20日均量: {sum(vols[-5:])/5:.0f} / {sum(vols[-20:])/20:.0f}  量比{sum(vols[-5:])/5/(sum(vols[-20:])/20):.2f}")
