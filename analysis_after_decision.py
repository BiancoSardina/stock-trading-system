#!/usr/bin/env python3
"""裁决后技术分析（手动触发，2026-10-18 拆分自 pool_then_analysis.py）。

背景：股票池任务产出 decision_bundle_latest.json，需要**外部 AI 裁决**（开发机），
裁决结果回写 watchlist.json 后才轮到技术分析。原 11:30 / 15:30 串联任务在池发布
成功后立刻跑分析 → 分析永远读不到本轮裁决名单。故拆分为：
  定时任务 = stock_pool_full.py / stock_pool_evening.py（只出池 + 裁决包 + 归档 + QQ 通知）
  手动任务 = 本脚本（外部裁决回写 watchlist 之后，由用户/agent 触发）

流程（全程只读 + 一次分析，不改池、不写裁决）：
  1. 校验池指针 pool_batch_latest.json 是否为当日正式批次（降级研究轮不动指针 → 默认拒绝）
  2. 同步远程裁决结果（默认开启，仅自动合并数据文件；远程含代码则告警不合并）
  3. 打印裁决名单新鲜度（watchlist.json updated / last_decision.generated_at / 只数）
  4. subprocess 跑 tech_analysis_bundle.py --task <午盘|盘后>技术分析
     报告全文落 chain_runs/analysis_<slot>_<ts>.log，stdout 只打摘要（便于 cron 存档阅读）

用法：
  python3 analysis_after_decision.py --slot midday [--no-sync] [--allow-stale-pool] [--check]
  两个 cron 手动入口（paused）用 analysis_midday.py / analysis_close.py 固定 slot，避免按小时猜错。
退出码：0 成功 / 2 参数或环境错误 / 3 池非当日且未放行 / 4 分析失败 / 5 同步失败 /
        6 启用 --require-adjudication 但裁决未回写
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from check_adjudication import adjudication_landed

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "chain_runs"
SLOTS = {"midday": "午盘技术分析", "close": "盘后技术分析"}
DATA_SUFFIXES = {".json", ".csv", ".txt", ".log"}
GIT_SSH = os.environ.get("GIT_SSH_COMMAND", "ssh -i /home/ubuntu/.ssh/id_ed25519_stock "
                         "-o BatchMode=yes -o ConnectTimeout=15")


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def resolve_slot(value: str) -> str:
    if value in SLOTS:
        return value
    # 手工直接跑时的兜底：13 点前当午盘，之后当盘后（与旧串联脚本一致）
    return "midday" if datetime.datetime.now().hour < 13 else "close"


def pool_batch() -> "tuple[dict | None, str]":
    try:
        data = json.loads((SCRIPT_DIR / "pool_batch_latest.json").read_text(encoding="utf-8"))
        return data, str(data.get("batch_id", ""))
    except Exception as exc:  # noqa: BLE001
        return None, f"读取指针失败: {exc}"


def batch_pool_summary(batch_id: str) -> dict:
    """池指针只有 batch_id/sha256，市场级与池成分要读批次目录里的 stock_pool.json。"""
    try:
        data = json.loads((SCRIPT_DIR / "pool_batches" / batch_id / "stock_pool.json").read_text(encoding="utf-8"))
        return {
            "market": f"{data.get('market_status', '?')}{data.get('market_score', '')}",
            "core": len(data.get("core_pool") or []),
            "watch": len(data.get("watch_pool") or []),
            "data_ok": data.get("data_ok"),
            "generated_at": data.get("generated_at", "?"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def watchlist_status() -> dict:
    try:
        data = json.loads((SCRIPT_DIR / "watchlist.json").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    decision = data.get("last_decision") or {}
    return {
        "ok": True,
        "updated": data.get("updated", "?"),
        "count": len(data.get("stocks") or []),
        "decided_at": decision.get("generated_at", "?"),
        "source_commit": str(decision.get("source_commit", ""))[:12],
    }


def sync_remote(verbose: bool = True) -> "tuple[bool, str]":
    """拉取裁决结果：只自动合并数据文件；远程含代码则告警不合并（沿用 2026-09-23 策略）。"""
    env = dict(os.environ, GIT_SSH_COMMAND=GIT_SSH)
    try:
        fetch = subprocess.run(["git", "fetch", "origin", "main"], cwd=SCRIPT_DIR,
                               capture_output=True, text=True, timeout=120, env=env)
    except subprocess.TimeoutExpired:
        return False, "git fetch 超时（120s）"
    if fetch.returncode != 0:
        return False, f"git fetch 失败: {(fetch.stderr or '').strip()[:300]}"
    behind = subprocess.run(["git", "rev-list", "--count", "HEAD..origin/main"], cwd=SCRIPT_DIR,
                            capture_output=True, text=True, env=env).stdout.strip()
    if behind in ("", "0"):
        return True, "远程无新提交"
    changed = subprocess.run(["git", "diff", "--name-only", "HEAD", "origin/main"], cwd=SCRIPT_DIR,
                             capture_output=True, text=True, env=env).stdout.split()
    code = [p for p in changed if Path(p).suffix.lower() not in DATA_SUFFIXES]
    if code:
        return True, f"远程领先 {behind} 个提交但含代码/配置({', '.join(code[:5])})，未自动合并"
    merge = subprocess.run(["git", "merge", "origin/main", "--no-edit", "-m",
                            f"同步远程裁决结果 {_now()}"], cwd=SCRIPT_DIR,
                           capture_output=True, text=True, timeout=120, env=env)
    if merge.returncode != 0:
        return True, f"远程领先 {behind} 个提交，自动合并失败（本地脏文件冲突？）：{(merge.stderr or '').strip()[:300]}"
    return True, f"已合并远程 {behind} 个提交（{len(changed)} 个数据文件）"


def run_analysis(slot: str, log_path: Path) -> int:
    task = SLOTS[slot]
    started = time.time()
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"\n[analysis] ▶ {task} 开始 {_now()}\n")
        fh.flush()
        p = subprocess.run([sys.executable, str(SCRIPT_DIR / "tech_analysis_bundle.py"),
                            "--task", task], cwd=str(SCRIPT_DIR), env=dict(os.environ),
                           stdout=fh, stderr=subprocess.STDOUT)
        elapsed = time.time() - started
        fh.write(f"[analysis] ■ 退出码 {p.returncode} 耗时 {elapsed:.0f}s {_now()}\n")
    return p.returncode


def bundle_summary() -> dict:
    try:
        data = json.loads((SCRIPT_DIR / "technical_analysis_latest.json").read_text(encoding="utf-8"))
        check = data.get("completeness") or {}
        pool = data.get("pool") or {}
        return {
            "ts": data.get("archive_ts", "?"),
            "verdict": check.get("verdict", "?"),
            "reason": str(check.get("reason", ""))[:160],
            "pool_mode": pool.get("mode", "?"),
            "report_lines": len(str(data.get("report", "")).splitlines()),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def main(slot_arg: "str | None" = None, argv: "list[str] | None" = None,
         require_adjudication_default: bool = False) -> int:
    parser = argparse.ArgumentParser(description="裁决后技术分析（手动触发）")
    parser.add_argument("--slot", default=slot_arg or "auto", choices=["auto", "midday", "close"])
    parser.add_argument("--no-sync", action="store_true", help="不拉取远程裁决结果")
    parser.add_argument("--allow-stale-pool", action="store_true",
                        help="允许用非当日的池批次出分析（默认拒绝）")
    parser.add_argument("--check", action="store_true", help="只做前置检查，不跑分析")
    parser.add_argument("--require-adjudication", dest="require_adjudication", action="store_true",
                        help="严格要求本轮裁决已回写（裁决时间须晚于当日池生成时间），否则 exit 6")
    parser.add_argument("--no-require-adjudication", dest="require_adjudication", action="store_false",
                        help="放宽：裁决未回写也照跑（只告警）")
    parser.set_defaults(require_adjudication=require_adjudication_default)
    args = parser.parse_args(argv)

    slot = resolve_slot(args.slot)
    task = SLOTS[slot]
    today = datetime.datetime.now().strftime("%Y%m%d")

    print(f"[裁决后分析] slot={slot} 任务={task} 时间={_now()}")

    # 1) 池批次闸门
    batch, batch_id = pool_batch()
    if not batch:
        print(f"[裁决后分析] ❌ {batch_id}；拒绝分析（避免用旧池出误导报告）")
        return 3
    is_today = batch_id[:8] == today
    summary = batch_pool_summary(batch_id)
    print(f"[裁决后分析] 池批次={batch_id or '?'} 当日={is_today} 池详情={summary}")
    if not is_today and not args.allow_stale_pool:
        print("[裁决后分析] ❌ 池指针不是当日正式批次（可能是降级研究轮/未发布）；"
              "如需用旧池分析请加 --allow-stale-pool")
        return 3

    # 2) 同步远程裁决结果
    if args.no_sync:
        print("[裁决后分析] 跳过远程同步（--no-sync）")
    else:
        ok, msg = sync_remote()
        print(f"[裁决后分析] 同步: {'✅' if ok else '❌'} {msg}")
        if not ok:
            return 5

    # 3) 裁决名单新鲜度
    wl = watchlist_status()
    if wl.get("ok"):
        print(f"[裁决后分析] 裁决名单 updated={wl['updated']} 只数={wl['count']} "
              f"裁决时间={wl['decided_at']} commit={wl['source_commit'] or '-'}")
        if str(wl["updated"]) < datetime.datetime.now().strftime("%Y-%m-%d"):
            tail = "── 已启用强校验，会被拦下（exit 6）" if args.require_adjudication else "（仍继续）"
            print("[裁决后分析] ⚠️ 裁决名单不是今天更新的：本轮外部裁决结果可能还没回写/同步，"
                  f"分析将不含新监测名单{tail}")
    else:
        print(f"[裁决后分析] ⚠️ 读取 watchlist.json 失败：{wl.get('error')}")

    if args.require_adjudication:
        landed = adjudication_landed(
            wl if wl.get("ok") else {"ok": False},
            {"ok": True, "generated_at": summary.get("generated_at", "") if isinstance(summary, dict) else ""},
        )
        if not landed:
            print(f"[裁决后分析] ❌ 本轮裁决未回写（裁决时间={wl.get('decided_at') or '?'} "
                  f"当日池生成={summary.get('generated_at') if isinstance(summary, dict) else '?'}）；"
                  "先做外部 AI 裁决并同步，或加 --no-require-adjudication 放宽")
            return 6
        print("[裁决后分析] ✅ 本轮裁决已回写（cron 手动入口默认强校验通过）")

    if args.check:
        print("[裁决后分析] --check：前置检查通过，未运行技术分析")
        return 0

    # 4) 技术分析
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"analysis_{slot}_{datetime.datetime.now():%Y%m%d%H%M%S}.log"
    print(f"[裁决后分析] 开始技术分析，日志: {log_path}")
    rc = run_analysis(slot, log_path)
    summary = bundle_summary()
    if rc == 0:
        print(f"[裁决后分析] ✅ 完成 {summary}")
        print(f"[裁决后分析] 报告全文: {log_path} "
              f"｜数据包: {SCRIPT_DIR / 'technical_analysis_latest.json'}")
    else:
        print(f"[裁决后分析] ❌ 技术分析失败 rc={rc} {summary}；详见 {log_path}")
    return 4 if rc else 0


if __name__ == "__main__":
    sys.exit(main())
