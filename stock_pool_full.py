#!/usr/bin/env python3
"""
股票池全流程串联（stock_pool_full.py）
=====================================
一天三次数据采集链路（早盘09:30 / 午盘12:30 / 尾盘14:20）：
  ① stock_pool.py 全量五因子生成 stock_pool.json（详细数据仅写后台）
  ② decision_bundle.py 运行确定性 Python 分析并生成可上传给外部 AI 的 JSON 数据包。
本链路不调用任何本地大模型、不写监测名单、不发送交易结论。
"""
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
    run_step("股票池生成", "stock_pool.py", {"PIPELINE_COMPACT": "1"})
    run_step("生成外部AI裁决数据包", "decision_bundle.py", {
        "PIPELINE_COMPACT": "1", "ANALYSIS_ONLY": "1", "BUNDLE_RUN_ANALYSIS": "1",
    })
    print(f"[stock_pool_full] 🎉 数据包生成完成 总耗时{time.time()-t_all:.0f}s", file=sys.stderr, flush=True)
