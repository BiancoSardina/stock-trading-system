#!/usr/bin/env python3
"""
bottom_scan.py — 底部窄幅横盘形态扫描（用户需求 2026-08-05）

形态定义（用户确认口径 v2）：
  ① 下跌很久：从阶段高点回撤 ≥ 30%（或60日跌幅 > 30%）
  ② 开始震荡：近期止跌，横盘
  ③ 震荡幅度：近10~20日 最高价/最低价 振幅 ≤ 10%（用户口径：10以内）
  ④ 震荡期不创新低：近10日最低价 > 下跌段最低价

用途：找出"跌透 + 横盘 = 吸筹末期/方向选择前"的标的，
     突破震荡上沿=买点，跌破下沿=继续阴跌。

用法：python3 bottom_scan.py [--top N]   # 默认输出前15只，按振幅升序
"""
import json, urllib.request, sys, os, time
from datetime import datetime

DRAW_DOWN_PCT = 0.30      # 回撤≥30%
RANGE_PCT = 0.10          # 20日振幅≤10%（用户口径：10以内）
RANGE_DAYS = 20           # 震荡观察窗口（近10~20日）
MIN_AMOUNT = 50_000_000   # 日成交额≥5000万（流动性）

def sina_get(url, gbk=True):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
    raw = urllib.request.urlopen(req, timeout=15).read()
    return raw.decode("gbk") if gbk else raw.decode("utf-8", "ignore")

def get_prefix(code):
    return "sh" if code.startswith(("5", "6", "9")) else "sz"

def get_kline(code, days=130):
    pref = get_prefix(code)
    url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={pref}{code}&scale=240&ma=5&datalen={days}"
    try:
        return json.loads(sina_get(url))
    except Exception:
        return None

def get_market_list(page, num=100):
    """新浪全市场A股列表，按涨幅排序分页"""
    url = (f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
           f"?page={page}&num={num}&sort=changepercent&asc=0&node=hs_a&symbol=&_s_r_a=page")
    try:
        return json.loads(sina_get(url))
    except Exception:
        return []

def is_main_board(code):
    """沪深主板白名单（用户可交易权限）"""
    return code.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))

def scan():
    """全市场扫描，返回符合底部窄幅横盘的标的列表"""
    candidates = []
    seen = set()
    print("📡 拉取全市场列表（按涨幅排序，分页）...", file=sys.stderr)
    for page in range(1, 26):  # 2500只
        stocks = get_market_list(page)
        if not stocks:
            break
        for s in stocks:
            code = str(s.get("code", ""))
            name = s.get("name", "")
            if not code or code in seen:
                continue
            seen.add(code)
            # 前置过滤
            if "ST" in name.upper() or "退" in name:
                continue
            if not is_main_board(code):
                continue
            try:
                amount = float(s.get("amount", 0) or 0)
                cur = float(s.get("trade", 0) or 0)
            except Exception:
                continue
            if amount < MIN_AMOUNT:
                continue
            candidates.append({"code": code, "name": name, "amount": amount, "cur": cur})
        time.sleep(0.12)
    print(f"  粗筛通过: {len(candidates)}只（主板+成交额≥5000万）", file=sys.stderr)

    # 逐只分析
    results = []
    for i, c in enumerate(candidates):
        kline = get_kline(c["code"])
        if not kline or len(kline) < 70:
            continue
        try:
            closes = [float(k["close"]) for k in kline]
            highs = [float(k["high"]) for k in kline]
            lows = [float(k["low"]) for k in kline]
            vols = [int(k["volume"]) for k in kline]
        except Exception:
            continue

        cur = c["cur"]
        # ① 下跌段：取120日最高点，计算回撤
        high_120 = max(highs)
        drawdown = (high_120 - cur) / high_120
        # 60日跌幅
        chg60 = (closes[-1] / closes[-61] - 1) if len(closes) >= 61 else None
        if drawdown < DRAW_DOWN_PCT and (chg60 is None or chg60 > -DRAW_DOWN_PCT):
            continue  # 跌幅不足

        # ② 震荡：近20日最高/最低振幅（用户口径：10以内）
        win_h = highs[-RANGE_DAYS:]
        win_l = lows[-RANGE_DAYS:]
        range_hi = max(win_h)
        range_lo = min(win_l)
        amp = range_hi / range_lo - 1
        if amp > RANGE_PCT:
            continue  # 振幅超10%

        # ③ 不创新低：近10日最低 > 下跌段最低（取近60日）
        low_60 = min(lows[-60:])
        if min(lows[-10:]) <= low_60 * 1.001:
            continue  # 仍在创新低，不算企稳

        # ④ 现价位置（区间内位置）：<30%下沿 / 30-70%中段 / >70%上沿
        pos = (cur - range_lo) / (range_hi - range_lo) * 100 if range_hi > range_lo else 50
        # 量能：近5日均量 vs 近20日均量（缩量横盘为佳）
        avgv5 = sum(vols[-5:]) / 5
        avgv20 = sum(vols[-20:]) / 20
        vol_ratio = avgv5 / avgv20 if avgv20 else 1
        # 近10日振幅（辅助，看更窄窗口是否已收窄）
        amp10 = max(highs[-10:]) / min(lows[-10:]) - 1

        results.append({
            "code": c["code"], "name": c["name"], "cur": cur,
            "amount": c["amount"],
            "drawdown": drawdown * 100, "chg60": (chg60 or 0) * 100,
            "range_hi": range_hi, "range_lo": range_lo,
            "amp": amp * 100, "amp10": amp10 * 100,
            "pos": pos, "vol_ratio": vol_ratio,
            "high_120": high_120, "low_60": low_60,
        })
        if (i + 1) % 100 == 0:
            print(f"  已分析 {i+1}/{len(candidates)}...", file=sys.stderr)

    # 按振幅升序（越窄越优先）
    results.sort(key=lambda x: x["amp"])
    return results

def main():
    top = 3  # 默认3只（用户口径：低风险横盘标的3只左右）
    if len(sys.argv) > 1 and sys.argv[1] == "--top":
        top = int(sys.argv[2])

    results = scan()
    print(f"\n{'='*68}")
    print(f"🔍 底部横盘扫描结果：{len(results)}只符合（回撤≥30% + 20日振幅≤10% + 不创新低）")
    print("🧾 手续费提醒：佣金万2.5最低5元/笔，单笔建议≥3000元(低于则不划算)；建仓按100股整数倍")
    print(f"{'='*68}")

    if not results:
        print("无符合条件的标的")
        return

    print(f"\n{'代码':<8}{'名称':<10}{'现价':>7}{'回撤':>7}{'60日':>7}{'20日幅':>7}{'10日幅':>7}{'区间':>12}{'位置':>6}{'量比':>6}")
    print("-" * 80)
    for r in results[:top]:
        pos_tag = "上沿" if r["pos"] >= 70 else "中段" if r["pos"] >= 30 else "下沿"
        vol_tag = "缩量" if r["vol_ratio"] < 0.8 else "平量" if r["vol_ratio"] < 1.2 else "放量"
        print(f"{r['code']:<8}{r['name'][:5]:<10}{r['cur']:>7.2f}"
              f"{r['drawdown']:>6.1f}%{r['chg60']:>6.1f}%{r['amp']:>6.2f}%{r['amp10']:>6.2f}%"
              f"{r['range_lo']:.2f}-{r['range_hi']:.2f}{pos_tag}{r['pos']:>5.0f}%{vol_tag}")

    print(f"\n📌 使用说明：")
    print(f"  · 突破 {results[0]['range_hi']:.2f}（震荡上沿）放量 → 吸筹完成信号，可介入")
    print(f"  · 跌破 {results[0]['range_lo']:.2f}（震荡下沿）→ 下跌中继，放弃")
    print(f"  · 现价位置越靠近下沿，回撤风险越小；越靠近上沿，越接近突破")
    print(f"  · 10日幅 < 20日幅 = 振幅正在收窄（横盘趋紧，接近变盘）")

    # 保存结果
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bottom_scan_result.txt"), "w", encoding="utf-8") as f:
            f.write(f"底部横盘扫描 {datetime.now().strftime('%Y-%m-%d %H:%M')} 共{len(results)}只\n")
            f.write("代码,名称,现价,回撤%,60日%,20日振幅%,10日振幅%,区间下沿,区间上沿,位置%,量比\n")
            for r in results:
                f.write(f"{r['code']},{r['name']},{r['cur']:.2f},{r['drawdown']:.1f},{r['chg60']:.1f},"
                        f"{r['amp']:.2f},{r['amp10']:.2f},{r['range_lo']:.2f},{r['range_hi']:.2f},{r['pos']:.0f},{r['vol_ratio']:.2f}\n")
        print(f"\n💾 结果已保存: bottom_scan_result.txt")
    except Exception:
        pass

if __name__ == "__main__":
    main()
