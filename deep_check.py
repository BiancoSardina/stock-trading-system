#!/usr/bin/env python3
"""通用单票/多票深度校验（前瞻式，含尾盘结论）

用法:
  python3 deep_check.py 002192 601016 ...
输出: 趋势三要件 / RS强度 / ATR / 动态支撑(回踩买) / 追涨位 / 破位弃 / 尾盘现价结论
数据源: 新浪优先，限流(HTTP 456)自动切东财
"""
import json, subprocess, sys, time, urllib.request
import board_policy
from datetime import datetime

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
EM_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


def http(url, gbk=True):
    # 东财 push2his 对 python urllib 会偶发 RemoteDisconnected，改用 curl 更稳
    if "eastmoney" in url:
        r = subprocess.run(["curl", "-s", "--max-time", "15",
                            "-H", "User-Agent: Mozilla/5.0",
                            "-H", "Referer: https://quote.eastmoney.com/", url],
                           capture_output=True)
        return r.stdout.decode("utf-8", "ignore")
    req = urllib.request.Request(url, headers=EM_UA if "eastmoney" in url else UA)
    raw = urllib.request.urlopen(req, timeout=15).read()
    return raw.decode("gbk", "ignore") if gbk else raw.decode("utf-8", "ignore")


def kline_sina(symbol, days=130):
    url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=5&datalen={days}")
    data = json.loads(http(url))
    return [{"day": x["day"], "close": float(x["close"]), "high": float(x["high"]),
             "low": float(x["low"]), "volume": int(x["volume"])} for x in data]


def kline_em_secid(secid, days=130):
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
           f"&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
           f"&klt=101&fqt=1&end=20500101&lmt={days}")
    d = None
    for attempt in range(3):
        try:
            d = json.loads(http(url, gbk=False))["data"]["klines"]
            if d:
                break
        except Exception:
            d = None
        time.sleep(1.5)
    if not d:
        raise RuntimeError(f"eastmoney kline empty for {secid}")
    out = []
    for line in d:
        p = line.split(",")
        out.append({"day": p[0], "close": float(p[2]), "high": float(p[3]),
                    "low": float(p[4]), "volume": int(float(p[5]))})
    return out


def kline_em(code, days=130):
    return kline_em_secid(board_policy.em_secid(code), days)


def get_kline(code, days=130):
    sym = board_policy.sina_prefix(code) + code
    try:
        return kline_sina(sym, days)
    except Exception:
        pass
    try:
        return kline_em(code, days)
    except Exception:
        return None


def realtime(code):
    sym = board_policy.sina_prefix(code) + code
    try:
        raw = http(f"https://hq.sinajs.cn/list={sym}")
        p = raw.split('"')[1].split(",")
        return {"name": p[0], "cur": float(p[3]), "high": float(p[4]),
                "low": float(p[5]), "prev_close": float(p[2]), "open": float(p[1]),
                "chg": (float(p[3]) / float(p[2]) - 1) * 100 if float(p[2]) else None}
    except Exception:
        pass
    try:
        secid = board_policy.em_secid(code)
        url = (f"https://push2.eastmoney.com/api/qt/ulist.np/get?secids={secid}"
               f"&fields=f12,f13,f14,f1,f2,f3,f15,f16,f17,f18")
        it = json.loads(http(url, gbk=False))["data"]["diff"][0]
        dec = 10 ** int(it.get("f1", 2) or 2)
        return {"name": it["f14"], "cur": it["f2"] / dec, "high": it["f15"] / dec,
                "low": it["f16"] / dec, "prev_close": it["f18"] / dec, "open": it["f17"] / dec,
                "chg": it["f3"] / 100 if it.get("f3") is not None else None}
    except Exception:
        return None


def calc_ma(c, n):
    return sum(c[-n:]) / n if len(c) >= n else None


def calc_atr(h, l, c, n=14):
    trs = [max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])) for i in range(1, len(c))]
    return sum(trs[-n:]) / n if len(trs) >= n else None


def idx_chg20():
    try:
        idx = kline_sina("sh000001", 30)
    except Exception:
        try:
            idx = kline_em_secid("1.000001", 30)
        except Exception:
            return None
    cl = [x["close"] for x in idx]
    return (cl[-1] / cl[-21] - 1) * 100 if len(cl) >= 21 else None


def main():
    codes = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not codes:
        print("用法: python3 deep_check.py 002192 601016 ...")
        return
    ib = idx_chg20()
    print(f"上证指数20日涨幅: {ib:+.2f}%" if ib is not None else "指数数据缺失")
    now = datetime.now()
    tail = (now.hour, now.minute) >= (14, 30) and now.hour < 15

    for code in codes:
        k = get_kline(code)
        rt = realtime(code)
        if not k:
            print(f"\n{code}: K线获取失败（新浪+东财均失败）")
            continue
        closes = [x["close"] for x in k]
        highs = [x["high"] for x in k]
        lows = [x["low"] for x in k]
        vols = [x["volume"] for x in k]
        today_str = now.strftime("%Y-%m-%d")
        has_today = k[-1].get("day") == today_str
        prev = closes[-2] if (has_today and len(closes) >= 2) else closes[-1]
        cur = rt["cur"] if rt and rt["cur"] > 0 else closes[-1]
        name = rt["name"] if rt else code
        th = max(rt["high"], highs[-1]) if rt and rt["high"] > 0 else highs[-1]
        tl = min(rt["low"], lows[-1]) if rt and rt["low"] > 0 else lows[-1]

        ma5, ma10, ma20, ma60 = (calc_ma(closes, n) for n in (5, 10, 20, 60))
        ma60_5 = calc_ma(closes[:-5], 60) if len(closes) >= 65 else None
        slope = (ma60 - ma60_5) / ma60_5 * 100 if ma60 and ma60_5 else None
        c1 = slope is not None and slope > 0
        c2 = ma60 is not None and cur > ma60
        c3 = ma20 is not None and ma60 is not None and ma20 > ma60
        three = c1 and c2 and c3

        chg20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 else None
        rs = chg20 - ib if chg20 is not None and ib is not None else None
        atr = calc_atr(highs, lows, closes)

        low5 = min(lows[-5:])
        dyn = max(low5, ma10) if ma10 else low5
        if (cur - dyn) / cur > 0.07:
            dyn = cur * 0.97
        high5 = max(highs[-5:] + [th])
        chase = high5 * 1.005
        low10 = min(lows[-10:] + [tl])
        volr = (sum(vols[-5:]) / 5) / (sum(vols[-20:]) / 20) if len(vols) >= 20 else 0
        pos = (cur - tl) / (th - tl) * 100 if th > tl else 50
        chg = rt["chg"] if rt and rt.get("chg") is not None else (cur / prev - 1) * 100

        print(f"\n{'='*68}")
        print(f"{code} {name}  现价{cur:.2f} ({chg:+.2f}%)  今高{th:.2f} 今低{tl:.2f} 日内位{pos:.0f}%")
        if ma5:
            print(f"MA5={ma5:.3f} MA10={ma10:.3f} MA20={ma20:.3f} MA60={ma60:.3f}")
        if slope is not None:
            print(f"MA60斜率{slope:+.2f}%  趋势三要件: {'满足' if three else '不满足'} "
                  f"[斜{'↑' if c1 else '↓'} 价{'上' if c2 else '下'}MA60 MA20{'上' if c3 else '下'}MA60]")
        if rs is not None:
            print(f"RS强度: 20日{'跑赢' if rs >= 0 else '跑输'}大盘 {rs:+.2f}% (标的{chg20:+.2f}% vs 大盘{ib:+.2f}%)")
        if atr:
            print(f"ATR14 {atr:.3f} ({atr/cur*100:.2f}%/日)  2倍ATR止损≈{atr*2:.3f}")
        print(f"动态支撑(回踩买): {dyn:.3f}  距现价{(cur-dyn)/cur*100:+.1f}%")
        print(f"追涨位: {chase:.3f}  距现价{(chase-cur)/cur*100:+.1f}%")
        print(f"破位弃: {low10:.3f}  距现价{(low10-cur)/cur*100:+.1f}%")
        print(f"量能: 5日均量{sum(vols[-5:])/5:.0f} / 20日均量{sum(vols[-20:])/20:.0f} = 量比{volr:.2f}")

        lo, hi = dyn, dyn * 1.02
        if tail:
            if three and lo <= cur <= hi:
                print(f">> 尾盘结论：现价买（{cur:.2f} 在买区 {lo:.2f}-{hi:.2f} 内，三要件满足），止损 {min(low10, cur*0.93):.2f}")
            elif three and cur < lo:
                print(f">> 尾盘结论：偏弱但在买区下方，挂 {lo:.2f}-{hi:.2f} 等回踩确认，破 {low10:.2f} 弃")
            elif three:
                print(f">> 尾盘结论：不追（三要件满足但现价高于买区），挂 {lo:.2f}-{hi:.2f} 等回踩；站上 {chase:.2f} 再考虑追")
            else:
                print(f">> 尾盘结论：不买（趋势三要件不满足，仅超跌/横盘，等 {chase:.2f} 放量突破再论）")
        else:
            print(f">> 预案：回踩 {lo:.2f}-{hi:.2f} 买 / 站上 {chase:.2f} 放量追 / 破 {low10:.2f} 放弃")


if __name__ == "__main__":
    main()
