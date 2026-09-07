#!/usr/bin/env python3
"""
tech_analysis_bundle.py — 盘中全量技术分析 → 数据包 → git 上传 → QQ 通知（2026-09-07 用户需求）

背景：原 11:10/13:30 定时任务（short_term_ai.py）跑全量分析后做 AI 解读推送。
用户 2026-09-07 指令：这两个时段**不做 AI 解读**，只做全量技术分析，
把分析结果打成数据包推送到 git，最后发 QQ 通知（提醒"XX 数据包上传完成"）。

流程：
  1. subprocess 跑 short_term.py 全量技术分析（ANALYSIS_ONLY=1 不写信号/状态、
     QQ_SEND_DISABLE=1 不直发 QQ），捕获 stdout 报告全文
  2. 把报告 + 元信息（当前股票池 date/market 摘要）写成 technical_analysis_latest.json
     （与 decision_bundle_latest.json 同级的"最新快照"固定名模式）
  3. subprocess 调 data_package_upload.py --task <时段任务名> --files technical_analysis_latest.json
     → 归档到 data_packages/technical_analysis_latest_<ts>.json → git push → QQ 提醒

任务名按运行时段自动推断：11 点→"收割后技术分析" / 13 点→"午后技术分析"，
也可 --task 手动覆盖。

⚠️ 与 v3.2 全家族不同：本脚本有 argparse（--help 安全）。cron no_agent 调用无需传参。
"""
import argparse
import json
import os
import subprocess
import sys
import time

from runtime import atomic_json, data_path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ANALYSIS_TIMEOUT = 360  # short_term.py 全量最长秒数（与 decision_bundle.py 默认一致）

SCHEMA = "technical-analysis-bundle/v1"


def infer_task() -> str:
    """按运行小时推断任务名（09:35 早盘 / 11:10 收割后 / 13:30 午后）"""
    hour = time.localtime().tm_hour
    if hour == 9:
        return "早盘技术分析"
    if hour == 11:
        return "收割后技术分析"
    if hour == 13:
        return "午后技术分析"
    return "盘中技术分析"


def run_analysis() -> tuple:
    """跑 short_term.py 全量分析（ANALYSIS_ONLY：只分析不写信号/不推QQ），返回 (stdout, stderr)"""
    script = os.path.join(SCRIPT_DIR, "short_term.py")
    env = dict(os.environ, ANALYSIS_ONLY="1", QQ_SEND_DISABLE="1", PYTHONUTF8="1")
    t0 = time.time()
    print(f"[tech_analysis_bundle] ⏱ 开始全量技术分析 {time.strftime('%H:%M:%S')}", file=sys.stderr, flush=True)
    proc = subprocess.run(
        [sys.executable, script],
        cwd=SCRIPT_DIR, env=env,
        capture_output=True, text=True, encoding="utf-8",
        timeout=ANALYSIS_TIMEOUT,
    )
    dur = time.time() - t0
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "未知错误")[-1000:]
        print(f"[tech_analysis_bundle] ❌ short_term.py 失败 exit={proc.returncode}: {detail}",
              file=sys.stderr, flush=True)
        sys.exit(proc.returncode)
    print(f"[tech_analysis_bundle] ✅ 全量技术分析完成 耗时{dur:.0f}s", file=sys.stderr, flush=True)
    return proc.stdout, proc.stderr


def pool_meta() -> dict:
    """读当前 stock_pool.json 的摘要元信息（容错：读不到返回空 dict）"""
    try:
        pool = json.load(open(data_path("stock_pool.json"), encoding="utf-8"))
        return {
            "date": pool.get("date"),
            "generated_at": pool.get("generated_at"),
            "market_status": pool.get("market_status"),
            "market_score": pool.get("market_score"),
            "data_ok": pool.get("data_ok"),
        }
    except Exception as exc:
        print(f"[tech_analysis_bundle] ⚠️ 读 stock_pool.json 元信息失败: {exc}", file=sys.stderr)
        return {}


def write_bundle(report: str, diagnostics: str) -> str:
    """把分析结果写成 technical_analysis_latest.json（供 data_package_upload 归档上传）"""
    bundle = {
        "schema": SCHEMA,
        "kind": "盘中全量技术分析（无AI解读，原始数据）",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "analysis_only": True,
        "local_ai_called": False,
        "pool": pool_meta(),
        "report": report,
        "diagnostics": (diagnostics or "")[-4000:],
    }
    out = data_path("technical_analysis_latest.json")
    atomic_json(out, bundle)
    size = os.path.getsize(out)
    print(f"[tech_analysis_bundle] 📦 分析数据包已生成 {out} ({size/1024:.1f}KB)", file=sys.stderr, flush=True)
    return out


def main():
    parser = argparse.ArgumentParser(description="盘中全量技术分析→数据包→git上传→QQ通知")
    parser.add_argument("--task", default="", help="任务名（默认按小时推断）")
    parser.add_argument("--skip-upload", action="store_true", help="只生成分析包，不归档上传（测试用）")
    args = parser.parse_args()

    task = args.task or infer_task()
    print(f"[tech_analysis_bundle] 🚀 任务: {task} {time.strftime('%Y-%m-%d %H:%M:%S')}", file=sys.stderr, flush=True)

    report, err = run_analysis()
    if not report.strip():
        print("[tech_analysis_bundle] ❌ short_term.py 输出为空，中止", file=sys.stderr)
        sys.exit(1)
    write_bundle(report, err)

    if args.skip_upload:
        print("[tech_analysis_bundle] skip-upload：未归档上传", file=sys.stderr)
        return

    # 复用 data_package_upload.py：归档 data_packages/ + git push + QQ 通知
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, "data_package_upload.py"),
         "--task", task, "--files", "technical_analysis_latest.json"],
        cwd=SCRIPT_DIR,
        timeout=180,
    )
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
