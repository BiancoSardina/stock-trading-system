#!/usr/bin/env python3
"""规则体检：信号等级/评分 vs 后续实际走势（胜率验证）"""
import csv, json, urllib.request, sys
from collections import defaultdict

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

rows = list(csv.DictReader(open('/home/ubuntu/.hermes/scripts/signal_log.csv', encoding='utf-8-sig')))
print(f"共 {len(rows)} 条信号\n")

# 按代码分组，取每个信号日期
by_code = defaultdict(list)
for r in rows:
    if r['操作'] == '买入' and r['价格']:
        try:
            by_code[r['代码']].append({
                'date': r['时间'][:10], 'price': float(r['价格']),
                'level': r['信号等级'], 'score': r['评分'],
                'mkt': r['市场状态'], 'status': r['状态']
            })
        except ValueError:
            pass

# 逐代码拉K线，匹配信号日后3/5日涨幅
stats = defaultdict(lambda: {'n': 0, 'win3': 0, 'win5': 0, 'sum3': 0.0, 'sum5': 0.0})
sample_miss = 0
for code, sigs in by_code.items():
    kline = get_kline(get_symbol(code))
    if not kline:
        continue
    kd = {k['day'][:10]: k for k in kline}
    days = [k['day'][:10] for k in kline]
    for s in sigs:
        d = s['date']
        if d not in kd:
            sample_miss += 1
            continue
        i = days.index(d)
        if i + 5 >= len(days):
            continue
        p0 = s['price']
        c3 = float(kline[min(i+3, len(days)-1)]['close'])
        c5 = float(kline[min(i+5, len(days)-1)]['close'])
        r3 = (c3/p0 - 1) * 100
        r5 = (c5/p0 - 1) * 100
        key = s['level']
        st = stats[key]
        st['n'] += 1
        if r3 > 0: st['win3'] += 1
        if r5 > 0: st['win5'] += 1
        st['sum3'] += r3
        st['sum5'] += r5

print("=" * 78)
print("📊 信号等级 vs 后续3/5日走势（按信号发出价计）")
print("=" * 78)
print(f"{'等级':<6}{'样本':>6}{'3日胜率':>10}{'5日胜率':>10}{'3日均涨':>10}{'5日均涨':>10}")
for lv in ['S', 'A', 'B', 'C', 'D']:
    st = stats.get(lv)
    if not st or st['n'] == 0:
        print(f"{lv:<6}{'0':>6}")
        continue
    print(f"{lv:<6}{st['n']:>6}{st['win3']/st['n']*100:>9.1f}%{st['win5']/st['n']*100:>9.1f}%"
          f"{st['sum3']/st['n']:>9.2f}%{st['sum5']/st['n']:>9.2f}%")
print(f"\n(未匹配到K线样本: {sample_miss}，多为最新信号/新股)")
