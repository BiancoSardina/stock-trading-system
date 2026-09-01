#!/usr/bin/env python3
"""
股票池V1.2 — 数据文件读写 + 生命周期管理（stock_pool_manager）
=============================================================
设计依据：stock_pool_design_v2.md

生命周期规则：
  · first_seen/days_in_pool：昨日池中今天仍在 → days+1 保留 first_seen；新进 → days=1
  · 默认保留 3 个交易日（days≤3 时入池门槛 -3 宽容；>3 严格按门槛）
  · 淘汰（任一满足）：total_score<70 / 现价<MA60（趋势破坏）/ industry_score<40（V1.2 放宽：行业"拖后腿"才淘汰）
  · 升级：watch 中 total_score≥85 → core
  · 降级：core 中 total_score<80 → watch
"""
import json
import os
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
POOL_PATH = os.path.join(SCRIPT_DIR, "stock_pool.json")

# 生命周期常量（V1.1 冲突点清单3：三处阈值口径独立，勿混用）
POOL_LEVEL_S = 85          # watch→core 升级线
POOL_LEVEL_CORE = 80       # core 下限（跌破降级 watch）
POOL_EVICT_SCORE = 70      # 淘汰线：总分
POOL_EVICT_INDUSTRY = 40   # 淘汰线：行业分（V1.2 从60放宽：行业"拖后腿"才淘汰，与准入线一致）
POOL_GRACE_DAYS = 3        # 保留期（宽容期）
POOL_GRACE_DEDUCT = 3      # 宽容期入池门槛下调分


def load_old_pool():
    """读旧 stock_pool.json，异常返回 None"""
    try:
        with open(POOL_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_pool(data):
    """写 stock_pool.json（原子写）"""
    tmp = POOL_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, POOL_PATH)


def old_lifecycle_map(old_pool):
    """
    旧池 → {code: {"first_seen": str, "days_in_pool": int}}
    用于新池继承生命周期
    """
    out = {}
    for grp in ("core_pool", "watch_pool"):
        for e in (old_pool or {}).get(grp, []):
            code = e.get("code")
            if code:
                out[code] = {
                    "first_seen": e.get("first_seen", ""),
                    "days_in_pool": int(e.get("days_in_pool", 1) or 1),
                }
    return out


def evict_check(total_score, industry_score, above_ma60):
    """
    淘汰判定（任一满足 True=应移出）：
      total_score<70 / 行业分<40（V1.2放宽）/ 现价<MA60（趋势破坏）
    注意：淘汰按严格值，不受宽容期影响（V1.1 冲突点清单5）
    """
    if total_score is not None and total_score < POOL_EVICT_SCORE:
        return True, f"总分{total_score}<{POOL_EVICT_SCORE}"
    if industry_score is not None and industry_score < POOL_EVICT_INDUSTRY:
        return True, f"行业{industry_score}<{POOL_EVICT_INDUSTRY}"
    if above_ma60 is False:
        return True, "现价破MA60"
    return False, ""


def entry_threshold(market_status, days_in_pool):
    """
    入池门槛（V1.1 修改一）：A75/B80/C82/D90（V1.3.3 2026-08-12：C级 85→82 缓解弱市空池）
    宽容期：旧池 days≤3 门槛 -3（只作用于入池判断，不作用于淘汰）
    """
    base = {"A": 75, "B": 80, "C": 82, "D": 90}.get(market_status, 80)
    if days_in_pool is not None and 0 < days_in_pool <= POOL_GRACE_DAYS:
        return base - POOL_GRACE_DEDUCT
    return base


def pool_capacity(market_status):
    """
    池子规模（V1.1 修改一，用户定稿 2026-08-07）：
      A: core12/watch20  B: core10/watch18  C: core8/watch16  D: core0/watch8
    """
    return {"A": (12, 20), "B": (10, 18), "C": (8, 16), "D": (0, 8)}.get(market_status, (10, 18))
