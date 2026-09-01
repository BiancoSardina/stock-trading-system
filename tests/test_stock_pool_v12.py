#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stock_pool V1.2 单测：generate_pool 配额/底线/趋势/准入逻辑（假数据，不拉行情）"""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
import stock_pool as sp

PASS, FAIL = 0, 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

def mk(code, name, total, stock, ind_score, above_ma20=True, ma20_gt_ma60=True, above_ma60=True, industry="行业A", watch_only=False, rs=12, capital=15):
    e = {
        "code": code, "name": name, "total_score": total, "stock_score": stock,
        "industry_score": ind_score, "industry": industry,
        "trend": {"above_ma20": above_ma20, "above_ma60": above_ma60, "ma20_gt_ma60": ma20_gt_ma60},
        "position": {"deduct": 0, "rise20": 10, "distance_ma20": 3, "rsi14": 60, "reasons": []},
        "factor": {"trend": 20, "momentum": 15, "capital": capital, "rs": rs, "risk": 8},
        "chg20": 10, "price": 10.0,
    }
    if watch_only:
        e["_watch_only"] = True
    return e

print("== 1. 行业配额：core 单行业最多2只 ==")
# 6只同行业高分票 → core 只进2只
scored = [mk(f"60000{i}", f"股{i}", 90 - i, 85 - i, 80) for i in range(6)]
core, watch, stats = sp.generate_pool(scored, "C", None, "2026-08-07")
check("core 单行业=2", len(core) == 2, f"len={len(core)}")
check("取总分最高的2只", [e["code"] for e in core] == ["600000", "600001"], str([e["code"] for e in core]))

print("== 2. 个股底线：core 需 stock_score≥70，watch 需 ≥60 ==")
scored = [
    mk("600100", "弱股", 88, 55, 80),     # 个股55 → 不够watch(60)
    mk("600101", "中等", 88, 65, 80),     # 个股65 → 可watch(≥60) 不可core(<70)
    mk("600102", "强股", 88, 75, 80),     # 个股75 → core
]
core, watch, stats = sp.generate_pool(scored, "C", None, "2026-08-07")
check("个股55→不进池", "600100" not in [e["code"] for e in core + watch], str([e["code"] for e in core + watch]))
check("个股65→watch", [e["code"] for e in watch] == ["600101"], str([e["code"] for e in watch]))
check("个股75→core", [e["code"] for e in core] == ["600102"], str([e["code"] for e in core]))

print("== 3. 趋势硬条件：core 需 价>MA20 且 MA20>MA60；watch 需 价>MA20 ==")
scored = [
    mk("600200", "趋势破", 88, 80, 80, above_ma20=True, ma20_gt_ma60=False),  # 不满足core
    mk("600201", "破MA20", 88, 80, 80, above_ma20=False, ma20_gt_ma60=True),  # 不满足core/watch
    mk("600202", "全满足", 88, 80, 80, above_ma20=True, ma20_gt_ma60=True),
]
core, watch, stats = sp.generate_pool(scored, "C", None, "2026-08-07")
check("MA20>MA60不成立→watch", "600200" in [e["code"] for e in watch] and "600200" not in [e["code"] for e in core],
      f"core={[e['code'] for e in core]} watch={[e['code'] for e in watch]}")
check("破MA20→不进池", "600201" not in [e["code"] for e in core + watch], str([e["code"] for e in core + watch]))
check("全满足→core", "600202" in [e["code"] for e in core], str([e["code"] for e in core]))

print("== 4. 行业准入：_watch_only 不能进 core ==")
scored = [
    mk("600300", "行业中等但强", 88, 80, 50, watch_only=True),  # 行业50 → 只能watch
    mk("600301", "行业强", 88, 80, 60),
]
core, watch, stats = sp.generate_pool(scored, "C", None, "2026-08-07")
check("行业50→watch不core", "600300" in [e["code"] for e in watch] and "600300" not in [e["code"] for e in core],
      f"core={[e['code'] for e in core]} watch={[e['code'] for e in watch]}")
check("行业60→core", [e["code"] for e in core] == ["600301"], str([e["code"] for e in core]))

print("== 5. 容量上限不填满（宁缺毋滥） ==")
scored = [mk(f"6004{i:02d}", f"股{i}", 60 + i, 60, 70) for i in range(3)]  # total 60-62 < C级门槛85
core, watch, stats = sp.generate_pool(scored, "C", None, "2026-08-07")
check("低分票不进池(不凑数)", len(core) == 0 and len(watch) == 0, f"core={len(core)} watch={len(watch)}")

print("== 6. D级市场：core 容量0 ==")
scored = [mk("600500", "强股", 95, 90, 85)]
core, watch, stats = sp.generate_pool(scored, "D", None, "2026-08-07")
check("D级 core=0", len(core) == 0, f"core={len(core)}")
check("D级 watch 可进", len(watch) == 1, f"watch={len(watch)}")

print("== 7. 生命周期继承：旧池票 days+1 ==")
old = {"core_pool": [dict(mk("600600", "旧股", 88, 80, 80), first_seen="2026-08-06", days_in_pool=2)]}
scored = [mk("600600", "旧股", 88, 80, 80)]
core, watch, stats = sp.generate_pool(scored, "C", old, "2026-08-07")
check("旧票 days=3", core and core[0]["days_in_pool"] == 3, str(core[0]["days_in_pool"]) if core else "无")
check("first_seen 保留", core and core[0]["first_seen"] == "2026-08-06", str(core))

print("== 8. 淘汰：总分<70 或 破MA60 ==")
old = {"core_pool": [
    dict(mk("600700", "低分", 65, 80, 80), first_seen="2026-08-05", days_in_pool=2),
    dict(mk("600701", "破位", 88, 80, 80), first_seen="2026-08-05", days_in_pool=2),
]}
scored = [
    mk("600700", "低分", 65, 80, 80),
    mk("600701", "破位", 88, 80, 80, above_ma60=False),
    mk("600702", "新票", 88, 80, 80),
]
core, watch, stats = sp.generate_pool(scored, "C", old, "2026-08-07")
check("低分淘汰", "600700" not in [e["code"] for e in core + watch], str(stats["evicted"]))
check("破MA60淘汰", "600701" not in [e["code"] for e in core + watch], str(stats["evicted"]))
check("淘汰统计记录", len(stats["evicted"]) == 2, str(stats["evicted"]))

print("== 9. CORE_STRONG 强者通道（C市）==")
# 9a: 强者票（个股90 行业70 RS16 资金19 综合80）→ core 且 level=strong（78门槛<普通85）
scored = [mk("600900", "强者", 80, 90, 70, rs=16, capital=19)]
core, watch, stats = sp.generate_pool(scored, "C", None, "2026-08-07")
check("强者票→core level=strong", len(core) == 1 and core[0].get("level") == "strong",
      str([(e["code"], e.get("level")) for e in core]))
# 9b: 普通强票（综合88 个股75 RS12<15）→ 普通core level=core
scored = [mk("600901", "普通强", 88, 75, 70, rs=12, capital=15)]
core, watch, stats = sp.generate_pool(scored, "C", None, "2026-08-07")
check("非强者→level=core", len(core) == 1 and core[0].get("level") == "core",
      str([(e["code"], e.get("level")) for e in core]))
# 9c: 强者综合79<82 可入（78门槛）；普通综合81<82 不可入（V1.3.3：C级门槛 85→82，用81验证"82以下不可入"）
scored = [
    mk("600902", "强79", 79, 90, 70, rs=16, capital=19),
    mk("600903", "普81", 81, 75, 70, rs=12, capital=15),
]
core, watch, stats = sp.generate_pool(scored, "C", None, "2026-08-07")
codes = [e["code"] for e in core]
check("强者78可入/普通82以下不可入", "600902" in codes and "600903" not in codes,
      str([(e["code"], e.get("level")) for e in core]))
# 9d: 强者容量≤3（4只强者跨4行业只进3，第4只落watch；同行业强者受单行业≤2配额限制）
scored = [mk(f"60091{i}", f"强{i}", 80, 90, 70, rs=16, capital=19, industry=f"行业{i}") for i in range(4)]
core, watch, stats = sp.generate_pool(scored, "C", None, "2026-08-07")
strongs = [e for e in core if e.get("level") == "strong"]
check("强者≤3只(跨行业)", len(strongs) == 3, str([(e["code"], e.get("level")) for e in core]))
# 9d2: 同行业强者只进2只（防垄断：strong与普通core共用单行业≤2配额）
scored = [mk(f"60095{i}", f"同{i}", 80, 90, 70, rs=16, capital=19) for i in range(4)]
core, watch, stats = sp.generate_pool(scored, "C", None, "2026-08-07")
strongs = [e for e in core if e.get("level") == "strong"]
check("同行业强者≤2只", len(strongs) == 2, str([(e["code"], e.get("level")) for e in core]))
# 9e: D市不开通道（禁买；D市强者票综合79<90 不进）
scored = [mk("600920", "D市强者", 79, 90, 70, rs=16, capital=19)]
core, watch, stats = sp.generate_pool(scored, "D", None, "2026-08-07")
check("D市不开强者通道", len(core) == 0, str([(e["code"], e.get("level")) for e in core]))
# 9e2: B市开通道（B市强者票综合79≥78 → strong；普通B市票80以下仍不进）
scored = [mk("600921", "B市强者", 79, 90, 70, rs=16, capital=19)]
core, watch, stats = sp.generate_pool(scored, "B", None, "2026-08-07")
check("B市强者通道开启", len(core) == 1 and core[0].get("level") == "strong",
      str([(e["code"], e.get("level")) for e in core]))
# 9f: 强者降级（RS跌到14<15 → 回落普通判断；综合88过85 → 普通core）
scored = [mk("600930", "降级", 88, 90, 70, rs=14, capital=19)]
core, watch, stats = sp.generate_pool(scored, "C", None, "2026-08-07")
check("RS14强者降级→普通core", len(core) == 1 and core[0].get("level") == "core",
      str([(e["code"], e.get("level")) for e in core]))
# 9g: 行业<65 不满足强者前置（行业60 综合80）→ 按普通判断（80<85不进）
scored = [mk("600940", "行业弱", 80, 90, 60, rs=16, capital=19)]
core, watch, stats = sp.generate_pool(scored, "C", None, "2026-08-07")
check("行业60不进强者通道", "600940" not in [e["code"] for e in core],
      str([(e["code"], e.get("level")) for e in core]))

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
