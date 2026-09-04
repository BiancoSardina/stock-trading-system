#!/usr/bin/env python3
"""生成供外部 AI 裁决使用的只读数据包，不调用任何大模型或发送消息。"""
import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from runtime import atomic_json, data_path
import position_manager
import stock_pool_manager as spm
import watchlist

SCHEMA = "external-ai-decision-bundle/v1"
MAX_AGE_SECONDS = 300


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
            "max_new_actions": 3,
            "must_prioritize_positions": True,
            "must_reject_stale_data": True,
            "must_reject_unknown_or_D_market_new_entries": True,
            "must_check_t_plus_one": True,
            "must_check_net_risk_reward": True,
            "note": "本数据包不包含AI结论；请上传给外部AI完成最终裁决。",
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
    output = args.output or data_path("decision_bundle_latest.json")
    atomic_json(output, bundle)
    print(f"[decision_bundle] 已生成 {output}｜有效至 {bundle['valid_until']}｜本地未调用AI", file=sys.stderr)


if __name__ == "__main__":
    main()
