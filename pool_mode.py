"""数据完整性分级 + 降级研究候选契约（用户 2026-09-22 确认的规则）。

原则：**软化研究流程，不软化数据真实性与交易风控。**
· 市场 UNKNOWN 不得改写为 C/D；不得用中性分伪装成完整市场评分；不得拿上一份市场等级冒充当前状态。
· 降级研究候选可以给条件区间/失效位/压力位，但**不构成"当前可以买"**：tradeable=false、不给手数、
  不写 signal_log/decision_state、不进正式裁决包、不写 watchlist.json。
· 降级轮不动正式池指针（上一批完整正式池继续生效）。

模式：
  正式     数据完整 → 走现有全部规则（正式池 + 裁决包 + 监测名单）
  降级研究 仅市场评分缺失 / 行业映射不完整 / 基准缺失，但个股与形态数据合格 → 另存研究候选
  停止     全市场列表不完整 / 行业严重残缺 / 候选行情失败过多 / 扫描未完成 → 整批停止，保留上一批正式池
"""
import json
from datetime import datetime, timedelta

from runtime import DATA_DIR

MODE_FORMAL = "正式"
MODE_DEGRADED = "降级研究"
MODE_STOP = "停止"

MODE_KEY = "pool_mode"
RESEARCH_SCHEMA = "research-candidates/v1"
RESEARCH_FILE = "research_candidates.json"          # 批次目录内（流水线）
RESEARCH_LATEST_FILE = "research_candidates_latest.json"  # 运行数据目录（供技术分析读取）
RESEARCH_LABEL = "降级研究候选"
RESEARCH_VALID_HOURS = 24          # 名义有效期（跨周末读取按 date 新鲜度 ≤4 天容忍）
MAX_POOL_STALE_DAYS = 4            # 与正式池一致的新鲜度容忍（覆盖周末/短假）

# 阈值（唯一定义处；改这里即可，勿在调用方硬编码）
MIN_INDUSTRIES_DEGRADED = 40       # 行业数 < 40 → 降级研究（行业分是 core 准入依据）
MIN_INDUSTRIES_STOP = 20           # 行业数 < 20 → 停止
MAX_FAILURE_RATIO = .02            # 候选行情失败率 > 2% → 停止
MIN_FAILURE_ALLOWANCE = 15

PROVENANCE_FORMAL = "正式池(完整)"
PROVENANCE_DEGRADED = "降级研究候选(市场UNKNOWN)"
PROVENANCE_FALLBACK = "回退固定自选"


def classify(*, market_status="UNKNOWN", market_data_ok=False, market_missing=(),
             fetch_complete=True, industry_count=0, industry_failed=(),
             benchmark_ok=False, candidate_failures=0, candidate_count=0, scan_complete=True):
    """按数据情况给出模式判定（stop 优先于 degraded）。返回 dict，供记录/报告/测试共用。"""
    market_missing = [str(x) for x in (market_missing or [])]
    industry_failed = [str(x) for x in (industry_failed or [])]
    stop, degraded = [], []
    if not fetch_complete:
        stop.append("全市场列表不完整")
    if not scan_complete:
        stop.append("评分阶段未完成")
    if not industry_count:
        stop.append("行业映射为空")
    elif industry_count < MIN_INDUSTRIES_STOP:
        stop.append(f"行业映射严重残缺（{industry_count}个 < {MIN_INDUSTRIES_STOP}）")
    allowance = max(MIN_FAILURE_ALLOWANCE, int(candidate_count * MAX_FAILURE_RATIO))
    if candidate_failures > allowance:
        stop.append(f"候选行情失败过多（{candidate_failures}/{candidate_count} > {allowance}）")
    if market_status not in ("A", "B", "C", "D") or not market_data_ok:
        degraded.append("市场评分不可用（保持 UNKNOWN，不猜测等级）")
    if market_missing:
        degraded.append("市场缺失子项：" + "；".join(market_missing))
    if industry_count < MIN_INDUSTRIES_DEGRADED or industry_failed:
        degraded.append(f"行业映射不完整（{industry_count}个行业，失败{len(industry_failed)}个）")
    if not benchmark_ok:
        degraded.append("基准指数缺失（相对强度不可用，RS 记 0 并标注）")
    mode = MODE_STOP if stop else (MODE_DEGRADED if degraded else MODE_FORMAL)
    return {"mode": mode, "stop_reasons": stop, "degraded_reasons": degraded,
            "reasons": stop or degraded,
            "market_status": market_status, "market_data_ok": bool(market_data_ok),
            "market_missing": market_missing, "fetch_complete": bool(fetch_complete),
            "industry_count": int(industry_count), "industry_failed": industry_failed,
            "benchmark_ok": bool(benchmark_ok),
            "candidate_failures": int(candidate_failures), "candidate_count": int(candidate_count),
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def is_degraded(mode):
    return mode == MODE_DEGRADED


def is_stop(mode):
    return mode == MODE_STOP


def freshness(date_str, now=None):
    """date 距今自然日数；解析失败返回 -1（与正式池口径一致）。"""
    now = now or datetime.now()
    try:
        return (now.date() - datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()).days
    except (TypeError, ValueError):
        return -1


def is_fresh(date_str, now=None, max_days=None):
    stale = freshness(date_str, now)
    return bool(str(date_str or "").strip()) and 0 <= stale <= (MAX_POOL_STALE_DAYS if max_days is None else max_days)


def research_write_path(batch_dir=None):
    """降级产物写入路径：流水线内写批次目录，独立运行写运行数据目录。"""
    import os
    from pathlib import Path
    override = batch_dir or os.environ.get("POOL_BATCH_DIR")
    if override:
        return str(Path(override) / RESEARCH_FILE)
    return str(DATA_DIR / RESEARCH_LATEST_FILE)


def build_research_payload(mode_record, *, date, core_pool, watch_pool, stats, scan_metrics,
                           formal_pool=None, batch_id=None, extra=None):
    """构造降级研究候选产物（schema=research-candidates/v1）。"""
    now = datetime.now()
    formal_pool = formal_pool or {}
    payload = {
        "schema": RESEARCH_SCHEMA,
        "mode": MODE_DEGRADED,
        "mode_label": RESEARCH_LABEL,
        "tradeable": False,
        "batch_id": batch_id,
        "date": date,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "valid_until": (now + timedelta(hours=RESEARCH_VALID_HOURS)).strftime("%Y-%m-%d %H:%M:%S"),
        "degraded_reasons": mode_record.get("degraded_reasons", []),
        "data_state": {k: mode_record.get(k) for k in
                       ("market_status", "market_data_ok", "market_missing", "industry_count",
                        "industry_failed", "benchmark_ok", "candidate_failures", "candidate_count")},
        # 市场保持 UNKNOWN：绝不改写成 C/D，也不用中性分伪装完整评分
        "market": {"state": "UNKNOWN", "score": None, "data_ok": False,
                   "missing": mode_record.get("market_missing", [])},
        "formal_pool": {"batch_id": formal_pool.get("batch_id"), "date": formal_pool.get("date"),
                        "generated_at": formal_pool.get("generated_at"),
                        "market_status": formal_pool.get("market_status"),
                        "note": "降级轮不更新正式池；此为当前仍在生效的上一批完整正式池"},
        "core_pool": core_pool,
        "watch_pool": watch_pool,
        "stats": stats,
        "scan_metrics": scan_metrics,
        "upgrade_requires": "下一次市场与个股数据完整复核；研究候选不得自行升级为买点",
        "not_a_buy_signal": True,
    }
    payload.update(extra or {})
    return payload


def validate_research_payload(payload):
    """降级产物校验：缺任一关键标注即拒绝（供批次发布与归档共用）。"""
    if not isinstance(payload, dict):
        raise ValueError("降级研究候选格式错误")
    if payload.get("schema") != RESEARCH_SCHEMA:
        raise ValueError("降级研究候选 schema 错误")
    if payload.get("mode") != MODE_DEGRADED:
        raise ValueError(f"mode 必须为 {MODE_DEGRADED}")
    if payload.get("tradeable") is not False or payload.get("not_a_buy_signal") is not True:
        raise ValueError("降级研究候选必须显式 tradeable=false / not_a_buy_signal=true")
    market = payload.get("market") or {}
    if market.get("state") != "UNKNOWN":
        raise ValueError("降级研究候选的市场状态必须保持 UNKNOWN（不得改写成 C/D）")
    if not payload.get("degraded_reasons"):
        raise ValueError("降级研究候选必须写明 degraded_reasons")
    if not is_fresh(payload.get("date")):
        raise ValueError(f"降级研究候选不是近期数据（date={payload.get('date')}）")
    return payload


def read_research(now=None, path=None):
    """读降级研究候选（默认运行数据目录的最新件）；不可用返回 None。"""
    from pathlib import Path
    now = now or datetime.now()
    target = Path(path) if path else (DATA_DIR / RESEARCH_LATEST_FILE)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        validate_research_payload(payload)
    except (OSError, ValueError):
        return None
    payload["_source"] = str(target)
    payload["_expired_nominal"] = str(payload.get("valid_until", "")) < now.strftime("%Y-%m-%d %H:%M:%S")
    return payload


def pool_provenance(formal_pool=None, now=None):
    """技术分析侧来源判定：正式池新鲜 → 正式；否则有可用降级候选 → 降级；否则回退自选。"""
    from runtime import data_path
    now = now or datetime.now()
    pool = formal_pool
    if pool is None:
        try:
            pool = json.loads(open(data_path("stock_pool.json"), encoding="utf-8").read())
        except (OSError, ValueError):
            pool = {}
    pool = pool or {}
    if pool and is_fresh(pool.get("date"), now) and (pool.get("mode", MODE_FORMAL) == MODE_FORMAL):
        return {"provenance": PROVENANCE_FORMAL, "degraded": False, "tradeable": True,
                "source": "stock_pool.json", "date": str(pool.get("date"))[:10],
                "stale_days": freshness(pool.get("date"), now), "reasons": []}
    research = read_research(now=now)
    if research:
        return {"provenance": PROVENANCE_DEGRADED, "degraded": True, "tradeable": False,
                "source": research["_source"], "date": research.get("date"),
                "stale_days": freshness(research.get("date"), now),
                "reasons": research.get("degraded_reasons", []),
                "nominal_expired": research.get("_expired_nominal", False),
                "valid_until": research.get("valid_until"),
                "note": "降级研究候选：可给区间/失效位/压力，但禁止买入，未升级"}
    return {"provenance": PROVENANCE_FALLBACK, "degraded": False, "tradeable": False,
            "source": "", "date": "", "stale_days": -1,
            "reasons": ["正式股票池与降级研究候选均不可用"]}
