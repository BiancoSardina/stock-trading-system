#!/usr/bin/env python3
"""规则体检4：状态机字段污染定位 + 卖出写入逻辑检查"""
import csv, json
from collections import Counter

rows = list(csv.DictReader(open('/home/ubuntu/.hermes/scripts/signal_log.csv', encoding='utf-8-sig')))

print("=" * 80)
print("🔍 状态机字段污染：新动作=标的名 的行")
print("=" * 80)
for r in rows:
    na = r.get('新动作', '')
    if na and na not in ('买入', '加仓', '持有', '卖出', '减仓', '清仓', ''):
        print(f"  {r['时间']} {r['代码']} {r['名称']} 操作={r['操作']} 旧动作={r.get('旧动作')!r} 新动作={na!r} 状态={r['状态']}")

print("\n" + "=" * 80)
print("🔍 V3.0 字段行统计（15列升级后的行）")
print("=" * 80)
has_v3 = [r for r in rows if r.get('旧动作') is not None or r.get('策略版本') == 'v3']
print(f"含V3.0字段的行: {len(has_v3)} / {len(rows)}")
# 看最近的 V3.0 行
for r in rows[-15:]:
    print(f"  {r['时间']} {r['代码']} {r['名称']} 操作={r['操作']} 等级={r['信号等级']} 状态={r['状态']} 旧={r.get('旧动作','')} 新={r.get('新动作','')}")

print("\n" + "=" * 80)
print("🔍 近期买入信号（08-10之后）——看现在系统还在输出什么")
print("=" * 80)
recent = [r for r in rows if r['时间'] >= '2026-08-10']
print(f"08-10后信号: {len(recent)} 条")
for r in recent:
    print(f"  {r['时间']} {r['代码']} {r['名称']} 操作={r['操作']} 等级={r['信号等级']} 评分={r['评分']} 市场={r['市场状态']} 状态={r['状态']}")

print("\n" + "=" * 80)
print("🔍 signal_log 写入逻辑（short_term.py 中 SIGNAL_LOG 相关）")
print("=" * 80)
