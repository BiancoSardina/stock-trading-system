#!/usr/bin/env python3
"""外部 AI 裁决数据包：不调用本地模型，且只接受新鲜股票池。"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_bundle as db

PASS = FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


now = datetime(2026, 9, 4, 10, 0, 0)
pool = {
    "date": "2026-09-04", "generated_at": "2026-09-04 09:57:00", "data_ok": True,
    "market_status": "B", "market_score": 70, "core_pool": [{"code": "600001"}], "watch_pool": [],
}
check("当天且五分钟内的完整股票池可用", db.validate_pool(pool, now) == datetime(2026, 9, 4, 9, 57, 0))

expired = dict(pool, generated_at="2026-09-04 09:50:00")
try:
    db.validate_pool(expired, now)
    check("过期股票池拒绝", False)
except ValueError:
    check("过期股票池拒绝", True)

bundle = db.build_bundle(pool, {"stock": [], "etf": []}, {"stocks": []}, {"report": "分析"}, now)
check("数据包明确不调用本地AI", bundle["integrity"]["local_ai_called"] is False)
check("数据包有五分钟有效期", bundle["valid_until"] == "2026-09-04 10:05:00")
check("数据包包含持仓、候选和分析", all(key in bundle for key in ("positions", "stock_pool", "python_analysis")))

source = (Path(__file__).resolve().parents[1] / "stock_pool_full.py").read_text(encoding="utf-8")
check("主流程不再调用股票池AI裁决", "stock_pool_ai.py" not in source)

print(f"结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
