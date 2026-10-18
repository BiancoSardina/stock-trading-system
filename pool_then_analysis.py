#!/usr/bin/env python3
"""⚠️ 2026-10-18 已拆分，本脚本仅作回滚保留（默认不要再挂 cron）。

拆分原因：股票池需要外部 AI 裁决（开发机），裁决回写 watchlist.json 后才该跑技术分析；
原串联在池发布成功后立刻分析，导致分析永远读不到本轮裁决名单。现架构：
  定时 = stock_pool_full.py / stock_pool_evening.py（只出池 + 裁决包 + 归档 + QQ）
  手动 = analysis_midday.py / analysis_close.py（裁决后触发 tech_analysis_bundle）
回滚：把两个 cron 的 script 改回本文件即可恢复"池→分析"串联。

股票池 → 技术分析 串联任务（午盘 11:30 / 盘后 15:30 共用）。

时段判定（按运行小时自动，`--slot` 可覆盖）：
  midday（hour < 13，cron 11:30）：
      stock_pool_full.py（任务名"午盘股票池"）→ 发布成功后
      tech_analysis_bundle.py --task 午盘技术分析
  close（hour >= 13，cron 15:30）：
      stock_pool_evening.py（任务名"收盘股票池"）→ 发布成功后
      tech_analysis_bundle.py --task 盘后技术分析

为什么串联而不是两个独立 cron：全A股入池口径下池链耗时未定（周末实测候选 3088 只），
若池链跑慢，下一个独立 cron 会读到上一批旧池 → 串联保证"池发布成功之后才分析"。

失败即止（no_agent cron 语义：stdout 非空会原文推送，故成功时保持静默）：
  池链失败                              → 不跑分析，exit 2
  池指针不是当日批次（降级研究/未发布）  → 跳过分析，exit 3
  分析链失败                            → exit 4
日志：chain_runs/<ts>_<slot>.log
env：PIPELINE_TIMEOUT 透传给池链（池链自身默认 2400）
用法：python3 pool_then_analysis.py [--slot auto|midday|close]
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "chain_runs"

# slot -> (池链脚本, 分析包任务名, 中文标签)
SLOTS = {
    "midday": ("stock_pool_full.py", "午盘技术分析", "午盘"),
    "close": ("stock_pool_evening.py", "盘后技术分析", "盘后"),
}


def resolve_slot(value):
    if value in SLOTS:
        return value
    return "midday" if datetime.datetime.now().hour < 13 else "close"


def _log_path(slot):
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    return LOG_DIR / f"{ts}_{slot}.log"


def _run_step(script, log_fh, extra_args=None):
    started = datetime.datetime.now()
    cmd = [sys.executable, str(SCRIPT_DIR / script)] + list(extra_args or [])
    log_fh.write(f"\n[chain] ▶ {script} {' '.join(extra_args or [])} 开始 {started:%H:%M:%S}\n")
    log_fh.flush()
    p = subprocess.run(
        cmd,
        cwd=str(SCRIPT_DIR),
        env=dict(os.environ),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    elapsed = (datetime.datetime.now() - started).total_seconds()
    log_fh.write(f"[chain] ■ {script} 退出码 {p.returncode} 耗时 {elapsed:.0f}s\n")
    log_fh.flush()
    return p.returncode, elapsed


def _batch_is_today():
    """池指针必须是"今天"的批次：降级研究轮不动指针，此时不应拿旧池出分析报告。"""
    try:
        with open(SCRIPT_DIR / "pool_batch_latest.json", encoding="utf-8") as f:
            batch = json.load(f)
        batch_id = str(batch.get("batch_id", ""))
        return batch_id[:8] == datetime.datetime.now().strftime("%Y%m%d"), batch_id
    except Exception as exc:  # noqa: BLE001
        return False, f"读取指针失败: {exc}"


def main():
    parser = argparse.ArgumentParser(description="股票池 → 技术分析串联（午盘/盘后）")
    parser.add_argument("--slot", default="auto", choices=["auto", "midday", "close"],
                        help="时段（默认 auto：13 点前=midday，之后=close）")
    args = parser.parse_args()

    slot = resolve_slot(args.slot)
    pool_script, analysis_task, label = SLOTS[slot]
    log_path = _log_path(slot)

    with open(log_path, "a", encoding="utf-8") as log_fh:
        log_fh.write(f"[chain] slot={slot} 池={pool_script} 分析任务={analysis_task}\n")

        pool_rc, pool_sec = _run_step(pool_script, log_fh)
        if pool_rc != 0:
            log_fh.write("[chain] 池链失败 → 跳过技术分析（避免用旧批次出报告）\n")
            print(f"[{label}串联] 股票池失败(退出码{pool_rc})，已跳过技术分析。日志: {log_path}")
            return 2

        today, batch_id = _batch_is_today()
        if not today:
            log_fh.write(f"[chain] 池指针非当日批次({batch_id}) → 跳过技术分析\n")
            print(f"[{label}串联] 股票池未发布当日正式批次(指针={batch_id})，已跳过技术分析。日志: {log_path}")
            return 3

        ana_rc, ana_sec = _run_step(
            "tech_analysis_bundle.py", log_fh, extra_args=["--task", analysis_task]
        )
        log_fh.write(
            f"[chain] 完成 slot={slot} 池={pool_sec:.0f}s 分析={ana_sec:.0f}s "
            f"池rc={pool_rc} 分析rc={ana_rc}\n"
        )

    if ana_rc != 0:
        print(f"[{label}串联] 技术分析失败(退出码{ana_rc})。日志: {log_path}")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
