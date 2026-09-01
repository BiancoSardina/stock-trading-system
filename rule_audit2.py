#!/usr/bin/env python3
"""规则体检2：D级信号明细 + 市场状态分层 + 评分区间胜率"""
import csv, json, urllib.request
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

# ===== 1. D级信号明细 =====
print("=" * 80)
print("🔍 D级信号明细（反常：胜率100%）")
print("=" * 80)
d_rows = [r for r in rows if r['信号等级'] == 'D' and r['操作'] == '买入']
print(f"D级买入信号共 {len(d_rows)} 条:")
for r in d_rows:
    print(f"  {r['时间']} {r['代码']} {r['名称']} 价{r['价格']} 评分{r['评分']} 市场{r['市场状态']} 状态{r['状态']}")

# ===== 2. 市场状态分层胜率 =====
print("\n" + "=" * 80)
print("📊 市场状态 × 等级 分层胜率")
print("=" * 80)
by_code = defaultdict(list)
for r in rows:
    if r['操作'] == '买入' and r['价格']:
        try:
            by_code[r['代码']].append({
                'date': r['时间'][:10], 'price': float(r['价格']),
                'level': r['信号等级'], 'mkt': r['市场状态']
            })
        except ValueError:
            pass

mkt_level = defaultdict(lambda: {'n': 0, 'win3': 0, 'win5': 0, 'sum5': 0.0})
for code, sigs in by_code.items():
    kline = get_kline(get_symbol(code))
    if not kline:
        continue
    kd = {k['day'][:10]: k for k in kline}
    days = [k['day'][:10] for k in kline]
    for s in sigs:
        d = s['date']
        if d not in kd:
            continue
        i = days.index(d)
        if i + 5 >= len(days):
            continue
        p0 = s['price']
        c5 = float(kline[i+5]['close'])
        r5 = (c5/p0 - 1) * 100
        key = f"{s['mkt'] or '?'}-{s['level']}"
        st = mkt_level[key]
        st['n'] += 1
        if r5 > 0: st['win5'] += 1
        st['sum5'] += r5

print(f"{'市场-等级':<12}{'样本':>6}{'5日胜率':>10}{'5日均涨':>10}")
for k in sorted(mkt_level.keys()):
    st = mkt_level[k]
    if st['n'] == 0: continue
    print(f"{k:<12}{st['n']:>6}{st['win5']/st['n']*100:>9.1f}%{st['sum5']/st['n']:>9.2f}%")

# ===== 3. 卖出/减仓信号 =====
print("\n" + "=" * 80)
print("🔍 卖出/减仓信号（仅31条 vs 买入877条——卖出信号是否缺失？）")
print("=" * 80)
sell_rows = [r for r in rows if r['操作'] == '卖出/减仓']
for r in sell_rows:
    print(f"  {r['时间']} {r['代码']} {r['名称']} 价{r['价格']} 等级{r['信号等级']} 评分{r['评分']} 状态{r['状态']}")
