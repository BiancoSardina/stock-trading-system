#!/usr/bin/env python3
"""股票池收盘生成+AI裁决（18:00 定时，2026-08-27 新增）

与 stock_pool_full.py 的区别：只跑「生成 + AI裁决」两步，不带盘中分析。
18:00 收盘后数据质量最好：生成收盘版 stock_pool.json + 裁决 watchlist.json，
供给次日 09:35 早盘手动分析（short_term_manual.py）与 20:00 晚间任务读取。

SKIP_CLEANUP=0：18:00 收盘轮次正常清理昨日未买入监测名单
（对应原 17:30 生成 / 18:00 裁决 的收盘链路，2026-08-26 一条龙方案后独立出来）。
"""
import datetime
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def run_step(name, script, env_extra=None):
    t0 = time.time()
    print(f"[stock_pool_evening] ⏱ 开始 {name} {time.strftime('%H:%M:%S')}", flush=True)
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, script)],
        cwd=SCRIPT_DIR,
        env=env,
    )
    dur = time.time() - t0
    if proc.returncode != 0:
        print(f"[stock_pool_evening] ❌ {name} 失败 exit={proc.returncode}", flush=True)
        sys.exit(proc.returncode)
    print(f"[stock_pool_evening] ✅ {name} 完成 耗时{dur:.0f}s", flush=True)
    return dur


if __name__ == "__main__":
    t_all = time.time()
    print(f"[stock_pool_evening] 🚀 收盘生成+裁决 启动 {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    run_step("股票池生成", "stock_pool.py")
    # 18:00 收盘轮次：清理昨日未买入监测名单（SKIP_CLEANUP=0）
    run_step("AI裁决", "stock_pool_ai.py", {"SKIP_CLEANUP": "0"})
    print(f"[stock_pool_evening] 🎉 完成 总耗时{time.time()-t_all:.0f}s", flush=True)
