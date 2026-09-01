#!/usr/bin/env python3
"""规则体检5：重复写入明细 + 卖出写入逻辑"""
import csv
from collections import Counter

rows = list(csv.DictReader(open('/home/ubuntu/.hermes/scripts/signal_log.csv', encoding='utf-8-sig')))

print("=" * 80)
print("🔍 512170 医疗ETF 08-04 全部记录（×22 重复明细）")
print("=" * 80)
for r in rows:
    if r['代码'] == '512170' and r['时间'].startswith('2026-08-04'):
        print(f"  {r['时间']} 操作={r['操作']} 价={r['价格']} 等级={r['信号等级']} 评分={r['评分']} 状态={r['状态']}")

print("\n" + "=" * 80)
print("🔍 08-03~08-06 每天信号条数（正常应=5时段×标的数）")
print("=" * 80)
for d in ['2026-08-03', '2026-08-04', '2026-08-05', '2026-08-06', '2026-08-07']:
    day_rows = [r for r in rows if r['时间'].startswith(d)]
    times = Counter(r['时间'][11:16] for r in day_rows)
    print(f"  {d}: 共{len(day_rows)}条, 时段分布={dict(times)}")
