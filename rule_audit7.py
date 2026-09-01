#!/usr/bin/env python3
"""规则体检7：卖出信号消失机制验证 + 虚拟账户 + 状态机动作标签"""
import csv, json
from collections import Counter

# 1. 状态机动作标签映射
print("=" * 80)
print("🔍 decision_manager 动作标签映射（检查 _ACTION_LABEL 是否缺少 key）")
print("=" * 80)
try:
    import decision_manager as dm
    print("_ACTION_LABEL:", json.dumps(dm._ACTION_LABEL, ensure_ascii=False))
except Exception as e:
    print(f"import失败: {e}")

# 2. 决策状态：持仓标的的最近动作
print("\n" + "=" * 80)
print("🔍 decision_state.json 中持仓标的状态")
print("=" * 80)
try:
    ds = json.load(open('/home/ubuntu/.hermes/scripts/decision_state.json', encoding='utf-8'))
    for code in ['159516', '159858', '562800']:
        v = ds.get(code, {})
        print(f"  {code}: {json.dumps(v, ensure_ascii=False)[:200]}")
except Exception as e:
    print(f"读取失败: {e}")

# 3. decision_history 最近动作
print("\n" + "=" * 80)
print("🔍 decision_history.json 最近10条（看卖出类动作是否存在）")
print("=" * 80)
try:
    dh = json.load(open('/home/ubuntu/.hermes/scripts/decision_history.json', encoding='utf-8'))
    items = dh if isinstance(dh, list) else dh.get('history', [])
    for it in items[-10:]:
        print(f"  {it}")
except Exception as e:
    print(f"读取失败: {e}")

# 4. 虚拟账户状态
print("\n" + "=" * 80)
print("🔍 paper_positions.json（虚拟账户）")
print("=" * 80)
try:
    pp = json.load(open('/home/ubuntu/.hermes/scripts/paper_positions.json', encoding='utf-8'))
    print(f"cash={pp.get('cash')}, positions={len(pp.get('positions', []))}只")
    for p in pp.get('positions', [])[:13]:
        if isinstance(p, dict):
            print(f"  {p.get('code')} {p.get('name')}: {p.get('shares')}份 @{p.get('buy_price')}")
except Exception as e:
    print(f"读取失败: {e}")

# 5. 卖出信号消失 - 看 v2.31 有没有任何 sell
print("\n" + "=" * 80)
print("🔍 v2.31 统一口径后的卖出信号")
print("=" * 80)
v231_sell = [r for r in rows if r.get('策略版本','').startswith('v2.31') and r['操作'] == '卖出/减仓']
print(f"v2.31卖出信号: {len(v231_sell)}条")
for r in v231_sell:
    print(f"  {r['时间']} {r['代码']} {r['名称']} 价{r['价格']} 等级{r['信号等级']} 状态{r['状态']}")
