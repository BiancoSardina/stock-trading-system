#!/usr/bin/env python3
"""
股票池全流程串联（stock_pool_full.py）
=====================================
一天三次一条龙（早盘09:30 / 午盘12:30 / 尾盘14:20）：
  ① stock_pool.py 全量五因子生成 stock_pool.json（详细数据仅写后台）
  ② stock_pool_ai.py 四角色AI裁决 core → watchlist.json（裁决仅写后台）
  ③ short_term_ai.py 汇总并只推送一条精简最终决策
     —— 用户要求"股票池AI裁决过后就进行定时任务分析"（2026-08-26 方案一）
早盘/午盘（<14点）自动传 SKIP_CLEANUP=1：跳过监测名单清理（昨日监测今天还要跟踪），
尾盘 14:20 轮次才清理昨日名单。
下游零改动：short_term.py V1.6 读 stock_pool.json 全量逐只分析（core+watch），
short_term_ai/manual 自动带上当天新鲜池子。
"""
import datetime
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEADLINE = time.monotonic() + float(os.environ.get("PIPELINE_TIMEOUT", "900"))


def run_step(name, script, env_extra=None):
    if time.monotonic() >= DEADLINE:
        raise TimeoutError("流程超时，停止后续发布")
    t0 = time.time()
    print(f"[stock_pool_full] ⏱ 开始 {name} {time.strftime('%H:%M:%S')}", file=sys.stderr, flush=True)
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    # 透传 stdout/stderr：脚本内部 qq_send.push_or_stdout 负责推 QQ（成功静默/失败 stdout）
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, script)],
        cwd=SCRIPT_DIR,
        env=env,
        timeout=max(1, DEADLINE - time.monotonic()),
    )
    dur = time.time() - t0
    if proc.returncode != 0:
        print(f"[stock_pool_full] ❌ {name} 失败 exit={proc.returncode}", file=sys.stderr, flush=True)
        sys.exit(proc.returncode)
    print(f"[stock_pool_full] ✅ {name} 完成 耗时{dur:.0f}s", file=sys.stderr, flush=True)
    return dur


if __name__ == "__main__":
    t_all = time.time()
    print(f"[stock_pool_full] 🚀 股票池全流程启动 {time.strftime('%Y-%m-%d %H:%M:%S')}", file=sys.stderr, flush=True)
    # 早盘/午盘跳过监测名单清理；尾盘（14点后）才清理昨日名单
    skip_cleanup = "1" if datetime.datetime.now().hour < 14 else "0"
    run_step("股票池生成", "stock_pool.py", {"PIPELINE_COMPACT": "1"})
    run_step("AI裁决", "stock_pool_ai.py", {
        "SKIP_CLEANUP": skip_cleanup,
        "PIPELINE_SILENT": "1",
    })
    run_step("盘中分析", "short_term_ai.py")
    print(f"[stock_pool_full] 🎉 全流程完成 总耗时{time.time()-t_all:.0f}s", file=sys.stderr, flush=True)
