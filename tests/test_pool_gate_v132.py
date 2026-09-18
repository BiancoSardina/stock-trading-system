#!/usr/bin/env python3
"""模拟测试：验证 V1.3.2 门槛修复后 A/B/C/D 各市场等级的池分配行为"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stock_pool as sp

def mk(code, total, stock=80, ind=70, trend_ok=True, watch_only=False, rs=12, cap=15, old=False, industry="测试行业"):
    """构造一只评分股票"""
    return {
        "code": code, "name": f"测试{code}", "total_score": total,
        "stock_score": stock, "industry_score": ind,
        "factor": {"rs": rs, "capital": cap},
        "trend": {"above_ma20": trend_ok, "ma20_gt_ma60": trend_ok, "above_ma60": trend_ok},
        "position": {"deduct": 0, "reasons": []},
        "industry": industry,
        "days_in_pool": 2 if old else 1,
        "first_seen": "2026-08-11" if old else "2026-08-12",
        "_old": old, "_watch_only": watch_only,
    }

def run_case(level, entries, label):
    core, watch, stats = sp.generate_pool(entries, level, None, "2026-08-12")
    print(f"\n{'='*60}\n【{label}】{level}级  容量 core{sp.spm.pool_capacity(level)[0]}/watch{sp.spm.pool_capacity(level)[1]}")
    print(f"CORE {len(core)}只: " + ", ".join(f"{e['code']}({e['total_score']})" for e in core) or "(空)")
    print(f"WATCH {len(watch)}只: " + ", ".join(f"{e['code']}({e['total_score']})" for e in watch) or "(空)")
    # 校验规则
    cap_core, cap_watch = sp.spm.pool_capacity(level)
    core_min = {"A": 75, "B": 80, "C": 82, "D": 90}[level]
    ok = True
    if len(core) > cap_core: ok = False; print(f"  ❌ core 超容量 {len(core)}>{cap_core}")
    if len(watch) > cap_watch: ok = False; print(f"  ❌ watch 超容量 {len(watch)}>{cap_watch}")
    for e in core:
        if e["total_score"] < core_min and e["level"] != "strong":
            # strong 通道 78 < 85 是合法的（仅C级）
            if not (level == "C" and e["total_score"] >= 78):
                ok = False; print(f"  ❌ core 门槛: {e['code']} total={e['total_score']}<{core_min}")
    for e in watch:
        if e["total_score"] < 65:
            ok = False; print(f"  ❌ watch 底线: {e['code']} total={e['total_score']}<65")
    print("  ✅ 规则校验通过" if ok else "  ❌ 存在违规")
    assert ok, f"{level}市场股票池违反门槛或容量规则"
    return core, watch

# ===== A级（门槛75，core12/watch20，无强者通道）=====
entries_a = [
    mk("A01", 90, stock=88, ind=80, industry="半导体"),
    mk("A02", 82, stock=80, ind=70, industry="白酒"),
    mk("A03", 76, stock=75, ind=60, industry="银行"),
    mk("A04", 72, stock=70, ind=60, industry="地产"),
    mk("A05", 66, stock=62, ind=50, industry="电力"),
    mk("A06", 60, stock=58, ind=50, industry="煤炭"),
    mk("A07", 95, stock=92, ind=85, industry="光伏"),
    mk("A08", 80, stock=78, ind=65, industry="军工"),
]
print(">>> A级期望：core收A07/01/02/03/08(≥75)，watch收A04/05(65-74)，A06出局")
run_case("A", entries_a, "A级")

# ===== B级（门槛80，core10/watch18，强者通道开启）=====
entries_b = [
    mk("B01", 84, stock=82, ind=96, industry="地产"),
    mk("B02", 81, stock=85, ind=56, industry="医药"),
    mk("B03", 79, stock=81, ind=70, industry="家电"),
    mk("B04", 72, stock=70, ind=60, industry="机械"),
    mk("B05", 66, stock=62, ind=50, industry="化工"),
    mk("B06", 88, stock=90, ind=80, rs=18, cap=20, industry="锂电"),
    mk("B07", 60, stock=58, ind=50, industry="钢铁"),
    mk("B08", 77, stock=75, ind=70, industry="汽车"),
]
print(">>> B级期望：core收B06(strong)/B01/02(≥80)，watch收B03/04/05/08(65-79)，B07出局")
run_case("B", entries_b, "B级")

# ===== C级（门槛82，core8/watch16，强者通道开启）=====
entries_c = [
    mk("C01", 90, stock=88, ind=80, industry="半导体"),
    mk("C02", 86, stock=84, ind=75, industry="白酒"),
    mk("C03", 82, stock=86, ind=70, rs=16, cap=19, industry="光伏"),  # strong通道(78+) → strong进core
    mk("C04", 83, stock=80, ind=60, industry="银行"),                  # 82-84 普通票 → 新门槛82下进core（旧85进不了）
    mk("C05", 81, stock=78, ind=60, industry="地产"),                  # <82 → watch
    mk("C06", 70, stock=68, ind=55, industry="电力"),                  # → watch
    mk("C07", 64, stock=62, ind=50, industry="煤炭"),                  # <65 出局
    mk("C08", 78, stock=80, ind=65, industry="军工"),                  # <82 → watch
]
print(">>> C级期望：core收C01/02(≥82)+C03(strong78+)+C04(83新门槛)，watch收C05/06/08(<82)，C07出局")
run_case("C", entries_c, "C级")

# ===== D级（门槛90，core0/watch8，无强者通道，禁买）=====
entries_d = [
    mk("D01", 92, stock=90, ind=85, industry="半导体"),
    mk("D02", 88, stock=86, ind=75, industry="白酒"),
    mk("D03", 80, stock=78, ind=65, industry="银行"),
    mk("D04", 72, stock=70, ind=60, industry="地产"),
    mk("D05", 66, stock=62, ind=50, industry="电力"),
    mk("D06", 60, stock=58, ind=50, industry="煤炭"),
]
print(">>> D级期望：core空(cap=0)，watch收D01-05(≤8只)，D06出局")
run_case("D", entries_d, "D级")
