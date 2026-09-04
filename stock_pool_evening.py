#!/usr/bin/env python3
"""股票池收盘生成+外部AI裁决数据包（18:00 定时）。

只生成收盘股票池和数据包，不调用本地AI、不写监测名单。
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
    print(f"[stock_pool_evening] ⏱ 开始 {name} {time.strftime('%H:%M:%S')}", flush=True)
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, script)],
        cwd=SCRIPT_DIR,
        env=env,
        timeout=max(1, DEADLINE - time.monotonic()),
    )
    dur = time.time() - t0
    if proc.returncode != 0:
        print(f"[stock_pool_evening] ❌ {name} 失败 exit={proc.returncode}", flush=True)
        sys.exit(proc.returncode)
    print(f"[stock_pool_evening] ✅ {name} 完成 耗时{dur:.0f}s", flush=True)
    return dur


if __name__ == "__main__":
    t_all = time.time()
    print(f"[stock_pool_evening] 🚀 收盘股票池+数据包 启动 {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    run_step("股票池生成", "stock_pool.py")
    run_step("生成外部AI裁决数据包", "decision_bundle.py", {"BUNDLE_RUN_ANALYSIS": "1"})
    print(f"[stock_pool_evening] 🎉 完成 总耗时{time.time()-t_all:.0f}s", flush=True)
