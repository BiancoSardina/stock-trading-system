#!/usr/bin/env python3
"""生成供外部 AI 裁决使用的只读数据包，不调用任何大模型或发送消息。"""
import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from runtime import atomic_json, data_path
from pool_batch import write_path
import position_manager
import stock_pool_manager as spm
import watchlist

SCHEMA = "external-ai-decision-bundle/v1"
# 2026-09-07 用户要求：裁决时效 5分钟 → 24小时。股票池是候选质量输入，不承担逐笔下单，
# 外部AI裁决可在生成后 24 小时内完成（数据包 valid_until 同步 +24h）
MAX_AGE_SECONDS = 24 * 60 * 60


def _now():
    return datetime.now()


def validate_pool(pool, now=None):
    """只接受当天、完整性校验通过的股票池；不能用旧池构造交易裁决。"""
    now = now or _now()
    if not isinstance(pool, dict) or not pool.get("data_ok"):
        raise ValueError("股票池缺失或未通过数据完整性校验")
    if pool.get("date") != now.strftime("%Y-%m-%d"):
        raise ValueError("股票池不是当天数据")
    try:
        generated = datetime.strptime(pool["generated_at"], "%Y-%m-%d %H:%M:%S")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("股票池缺少生成时间") from exc
    age = (now - generated).total_seconds()
    if not 0 <= age <= MAX_AGE_SECONDS:
        raise ValueError(f"股票池已过期（{age:.0f}秒），请重新生成")
    return generated


def run_python_analysis(timeout):
    """用分析模式运行原有确定性分析，捕获结果写入数据包，不写信号或订单。"""
    script = Path(__file__).with_name("short_term.py")
    env = dict(os.environ, ANALYSIS_ONLY="1", QQ_SEND_DISABLE="1", PYTHONUTF8="1")
    proc = subprocess.run([sys.executable, str(script)], cwd=str(script.parent), env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if proc.returncode:
        detail = (proc.stderr or proc.stdout or "未知错误")[-1000:]
        raise RuntimeError(f"Python分析失败 exit={proc.returncode}: {detail}")
    return {"generated_at": _now().strftime("%Y-%m-%d %H:%M:%S"), "report": proc.stdout,
            "diagnostics": proc.stderr[-4000:] if proc.stderr else ""}


def build_bundle(pool, positions, current_watchlist, analysis, generated_at=None):
    """构造可上传的裁决输入。该包只有数据与规则，不含 AI 结论。"""
    generated_at = generated_at or _now()
    return {
        "schema": SCHEMA,
        "batch_id": pool.get("batch_id"),
        "generated_at": generated_at.strftime("%Y-%m-%d %H:%M:%S"),
        "valid_until": (generated_at + timedelta(seconds=MAX_AGE_SECONDS)).strftime("%Y-%m-%d %H:%M:%S"),
        "integrity": {
            "pool_data_ok": bool(pool.get("data_ok")),
            "analysis_only": True,
            "local_ai_called": False,
        },
        "market": {key: pool.get(key) for key in ("date", "generated_at", "market_status", "market_score")},
        "stock_pool": {
            "core_pool": pool.get("core_pool", []),
            "watch_pool": pool.get("watch_pool", []),
            "stats": pool.get("stats", {}),
        },
        "positions": positions,
        "existing_watchlist": current_watchlist,
        "python_analysis": analysis,
        "decision_contract": {
            "opportunity_version": "startup/v1",
            "candidate_paths": ["整理启动型", "超跌企稳型"],
            "budget_independent_research": True,
            "strength_grade_is_not_candidate_gate": True,
            "quality_classification_is_time_independent": True,
            "buy_state_is_point_in_time": True,
            "buy_states": ["条件满足", "等待", "失效"],
            "must_reject_yes_on_invalidated_buy_state": True,
            "max_new_actions": 3,
            "must_reject_stale_data": True,
            "must_reject_unknown_or_D_market_new_entries": True,
            "must_check_price_risk_reward_for_research": True,
            "must_check_net_risk_reward_for_orders": True,
            "positions_are_context_only": True,
            "note": "本数据包不包含AI结论；外部AI按opportunity形态证据裁决监测质量。"
                    "core/watch 是质量分级（只看形态与行业），不随扫描时点变化——午休/盘后扫描出的 core 有效，"
                    "不得因为非交易时段而降低其分类。每只候选的买点状态见 buy_state："
                    "条件满足=本轮闸门全过；等待=形态成立但未到区间/未收复开盘·昨收·MA5/非交易时段；"
                    "失效=形态不成立或已触及结构失效位。buy_state=失效的候选不得给出 YES（仅记录）。"
                    "两类候选不要求站上MA60或放量大涨，不因预设预算淘汰。D级可监测但禁止新买；UNKNOWN拒绝裁决。"
                    "仅引用包中条件区间，不编造价格；持仓仅作背景。",
        },
    }


def main():
    parser = argparse.ArgumentParser(description="生成外部AI裁决数据包")
    parser.add_argument("--run-analysis", action="store_true", help="执行 short_term.py 并把原始分析放入数据包")
    parser.add_argument("--timeout", type=int, default=360, help="Python分析最长秒数")
    parser.add_argument("--output", help="输出文件路径；默认写入运行数据目录")
    args = parser.parse_args()

    now = _now()
    pool = spm.load_old_pool()
    validate_pool(pool, now)
    if args.run_analysis or os.environ.get("BUNDLE_RUN_ANALYSIS") == "1":
        analysis = run_python_analysis(args.timeout)
    else:
        analysis = {"generated_at": now.strftime("%Y-%m-%d %H:%M:%S"), "report": "",
                    "diagnostics": "未运行 short_term.py；仅供收盘候选裁决。"}
    bundle = build_bundle(pool, position_manager.load_positions(), watchlist.load_watchlist(), analysis, _now())
    output = args.output or write_path("decision_bundle_latest.json")
    atomic_json(output, bundle)
    print(f"[decision_bundle] 已生成 {output}｜有效至 {bundle['valid_until']}｜本地未调用AI", file=sys.stderr)


if __name__ == "__main__":
    main()
