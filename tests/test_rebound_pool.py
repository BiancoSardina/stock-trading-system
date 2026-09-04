#!/usr/bin/env python3
"""ReboundPool 行业热度门槛：纯函数测试，不访问行情。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rebound_pool as rp

PASS = FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


print("== ReboundPool 行业热度门槛 ==")

hot, bonus, reason = rp.industry_heat({
    "score": 75, "up_ratio": .65, "avg_chg": .8, "period": (1.2, 4.0),
})
check("中强行业且资金广度正常→通过", hot and bonus == 12, str((hot, bonus, reason)))

hot, bonus, reason = rp.industry_heat({
    "score": 85, "up_ratio": .70, "avg_chg": .6, "period": (-.2, 5.0),
})
check("行业短期动量转弱→排除", not hot and bonus == 0 and "动量" in reason, str((hot, bonus, reason)))

hot, bonus, reason = rp.industry_heat({
    "score": 59, "up_ratio": .90, "avg_chg": 2., "period": (2., 8.),
})
check("行业分不足不能用强个股形态弥补", not hot and bonus == 0 and "行业分" in reason, str((hot, bonus, reason)))

out = rp.evaluate_stock("600000", "测试", 1e9, None, None)
check("行业数据缺失在取个股K线前直接排除", out.get("_exclude", "").startswith("行业不热"), str(out))

print(f"结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
