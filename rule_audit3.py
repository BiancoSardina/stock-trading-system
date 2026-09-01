#!/usr/bin/env python3
"""规则体检3：卖出信号缺失验证 + 信号重复写入 + 虚拟交易一致性"""
import csv, json
from collections import Counter, defaultdict

rows = list(csv.DictReader(open('/home/ubuntu/.hermes/scripts/signal_log.csv', encoding='utf-8-sig')))

print("=" * 80)
print("🔍 体检1：卖出/减仓信号时间分布")
print("=" * 80)
sell = [r for r in rows if r['操作'] == '卖出/减仓']
by_day = Counter(r['时间'][:10] for r in sell)
print("卖出信号按日期:", dict(sorted(by_day.items())))
print(f"最后一条卖出信号: {sell[-1]['时间'] if sell else '无'}")
# 08-06 之后还有卖出信号吗？
after = [r for r in sell if r['时间'] >= '2026-08-07']
print(f"08-07 之后的卖出信号: {len(after)} 条")

print("\n" + "=" * 80)
print("🔍 体检2：新动作分布（V3.0状态机字段，看卖出动作有没有被状态机输出）")
print("=" * 80)
new_act = Counter(r.get('新动作','') for r in rows if r.get('新动作'))
print("新动作:", dict(new_act))
old_act = Counter(r.get('旧动作','') for r in rows if r.get('旧动作'))
print("旧动作:", dict(old_act))

print("\n" + "=" * 80)
print("🔍 体检3：信号重复写入（同代码+同日期+同价格 出现多次）")
print("=" * 80)
key_cnt = Counter((r['时间'][:10], r['代码'], r['价格'], r['操作']) for r in rows)
dups = {k: v for k, v in key_cnt.items() if v > 1}
print(f"重复信号组数: {len(dups)}")
for k, v in sorted(dups.items(), key=lambda x: -x[1])[:10]:
    print(f"  {k[0]} {k[1]} 价{k[2]} {k[3]} ×{v}")

print("\n" + "=" * 80)
print("🔍 体检4：各代码信号统计（哪些标的被反复喊买入）")
print("=" * 80)
buy_by_code = Counter((r['代码'], r['名称']) for r in rows if r['操作'] == '买入')
for (code, name), n in buy_by_code.most_common(15):
    print(f"  {code} {name}: {n}次买入信号")

print("\n" + "=" * 80)
print("🔍 体检5：决策状态机当前状态（decision_state.json）")
print("=" * 80)
try:
    ds = json.load(open('/home/ubuntu/.hermes/scripts/decision_state.json', encoding='utf-8'))
    st_cnt = Counter()
    for code, v in ds.items():
        if isinstance(v, dict):
            st_cnt[v.get('state', '?')] += 1
    print(f"标的数: {len(ds)}  状态分布: {dict(st_cnt)}")
    for code, v in list(ds.items())[:8]:
        if isinstance(v, dict):
            print(f"  {code}: state={v.get('state')} last_action={v.get('last_action')} last_time={v.get('last_time','')[:16]}")
except Exception as e:
    print(f"读取失败: {e}")

print("\n" + "=" * 80)
print("🔍 体检6：虚拟账户 vs 实际持仓")
print("=" * 80)
try:
    pp = json.load(open('/home/ubuntu/.hermes/scripts/paper_positions.json', encoding='utf-8'))
    print(f"虚拟现金: {pp.get('cash')}  持仓数: {len(pp.get('positions', []))}")
    for p in pp.get('positions', []):
        print(f"  {p.get('code')} {p.get('name')}: {p.get('shares')}份 @{p.get('buy_price')}")
except Exception as e:
    print(f"读取失败: {e}")
try:
    pos = json.load(open('/home/ubuntu/.hermes/scripts/positions.json', encoding='utf-8'))
    print(f"实际持仓:")
    for g in ('etf', 'stock'):
        for p in pos.get(g, []):
            print(f"  {p['code']} {p['name']}: {p.get('shares','?')}份 @{p['buy_price']} 投入{p.get('amount')}")
except Exception as e:
    print(f"读取失败: {e}")
