#!/usr/bin/env python3
"""规则体检6：按策略版本分层统计（旧版legacy vs v2.31 vs V3.0）+ 评分口径确认"""
import csv, json, urllib.request
from collections import defaultdict, Counter

rows = list(csv.DictReader(open('/home/ubuntu/.hermes/scripts/signal_log.csv', encoding='utf-8-sig')))

print("=" * 80)
print("🔍 策略版本分布 + 每版本评分范围")
print("=" * 80)
ver_stat = defaultdict(list)
for r in rows:
    v = r.get('策略版本', 'legacy(无字段)')
    ver_stat[v].append(r)
for v, rs in ver_stat.items():
    scores = [float(r['评分']) for r in rs if r['评分']]
    print(f"  {v}: {len(rs)}条, 评分范围 [{min(scores):.0f}, {max(scores):.0f}], 平均{sum(scores)/len(scores):.1f}")

print("\n" + "=" * 80)
print("🔍 各版本 评分→等级 交叉（检测等级映射与评分是否一致）")
print("=" * 80)
for v, rs in ver_stat.items():
    pairs = Counter((r['评分'], r['信号等级']) for r in rs if r['评分'])
    print(f"  [{v}] 前8组 (评分,等级):")
    for (s, lv), n in pairs.most_common(8):
        print(f"    {s:>6} → {lv}: {n}条")

print("\n" + "=" * 80)
print("🔍 仅统计 v2.31 之后的信号（评分体系统一后）：等级 vs 胜率")
print("=" * 80)

def sina_get(url, gbk=True):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
    raw = urllib.request.urlopen(req, timeout=15).read()
    return raw.decode("gbk") if gbk else raw.decode("utf-8", "ignore")

def get_kline(symbol, days=40):
    url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=5&datalen={days}"
    try:
        return json.loads(sina_get(url))
    except Exception:
        return None

def get_symbol(code):
    return ("sh" if code.startswith(("5","6","9")) else "sz") + code

# 只看有策略版本字段且评分≥0的行（统一口径），且操作=买入
uni = [r for r in rows if r.get('策略版本') and r['策略版本'] != 'legacy' and r['操作'] == '买入' and r['价格']]
print(f"统一评分体系后的买入信号: {len(uni)}条")
by_code = defaultdict(list)
for r in uni:
    try:
        by_code[r['代码']].append({'date': r['时间'][:10], 'price': float(r['价格']),
                                   'level': r['信号等级'], 'score': float(r['评分']), 'mkt': r['市场状态']})
    except ValueError:
        pass

stats = defaultdict(lambda: {'n': 0, 'win3': 0, 'win5': 0, 'sum3': 0.0, 'sum5': 0.0})
for code, sigs in by_code.items():
    kline = get_kline(get_symbol(code))
    if not kline: continue
    kd = {k['day'][:10]: k for k in kline}
    days = [k['day'][:10] for k in kline]
    for s in sigs:
        d = s['date']
        if d not in kd: continue
        i = days.index(d)
        if i + 5 >= len(days): continue
        p0 = s['price']
        c3 = float(kline[i+3]['close']); c5 = float(kline[i+5]['close'])
        r3 = (c3/p0-1)*100; r5 = (c5/p0-1)*100
        st = stats[s['level']]
        st['n'] += 1
        if r3 > 0: st['win3'] += 1
        if r5 > 0: st['win5'] += 1
        st['sum3'] += r3; st['sum5'] += r5

print(f"{'等级':<6}{'样本':>6}{'3日胜率':>10}{'5日胜率':>10}{'3日均涨':>10}{'5日均涨':>10}")
for lv in ['S','A','B','C','D']:
    st = stats.get(lv)
    if not st or st['n'] == 0:
        print(f"{lv:<6}{'0':>6}"); continue
    print(f"{lv:<6}{st['n']:>6}{st['win3']/st['n']*100:>9.1f}%{st['win5']/st['n']*100:>9.1f}%"
          f"{st['sum3']/st['n']:>9.2f}%{st['sum5']/st['n']:>9.2f}%")

# 评分区间细分
print("\n🔍 评分区间 vs 5日胜率（统一口径买入信号）")
score_band = defaultdict(lambda: {'n': 0, 'win5': 0, 'sum5': 0.0})
for code, sigs in by_code.items():
    kline = get_kline(get_symbol(code))
    if not kline: continue
    kd = {k['day'][:10]: k for k in kline}
    days = [k['day'][:10] for k in kline]
    for s in sigs:
        d = s['date']
        if d not in kd: continue
        i = days.index(d)
        if i + 5 >= len(days): continue
        p0 = s['price']; c5 = float(kline[i+5]['close'])
        r5 = (c5/p0-1)*100
        band = f"{int(s['score']//10*10)}-{int(s['score']//10*10)+9}"
        st = score_band[band]
        st['n'] += 1
        if r5 > 0: st['win5'] += 1
        st['sum5'] += r5
for b in sorted(score_band.keys()):
    st = score_band[b]
    if st['n'] == 0: continue
    print(f"  评分{b}: n={st['n']:>4} 5日胜率={st['win5']/st['n']*100:>5.1f}% 5日均涨={st['sum5']/st['n']:>6.2f}%")
