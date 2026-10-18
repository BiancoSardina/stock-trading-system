#!/usr/bin/env python3
"""探测外部 AI 裁决是否已推送到 git（只读，不改本地文件、不合并、不跑分析）。

用途：拆分后技术分析必须等外部裁决回写 watchlist.json。本脚本回答三个问题：
  1) 当日股票池批次是否已发布（batch_id / generated_at）
  2) 远程 origin/main 上的裁决是否已到位（直接 git show origin/main:watchlist.json，
     不需要先合并到本机）
  3) 本机 watchlist 是否已同步（分析脚本会自动合并，这里只对比）

判定“裁决已到位”：裁决时间 >= 当日池批次生成时间，且日期=今天
（否则说明裁的是上一轮旧包）。远程到位但本机没同步时，跑分析会自动合并数据文件。

用法：
  python3 check_adjudication.py                 # 查一次
  python3 check_adjudication.py --wait 15       # 最多等 15 分钟（每 60s 查一次）
  python3 check_adjudication.py --json          # 机器可读输出
退出码：0 远程裁决已到位 / 1 尚未到位（或等待超时）/ 2 git 失败
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
GIT_SSH = os.environ.get("GIT_SSH_COMMAND", "ssh -i /home/ubuntu/.ssh/id_ed25519_stock "
                         "-o BatchMode=yes -o ConnectTimeout=15")
DATA_SUFFIXES = {".json", ".csv", ".txt", ".log"}


def _git(args, timeout=120):
    return subprocess.run(["git", *args], cwd=SCRIPT_DIR, capture_output=True, text=True,
                          timeout=timeout, env=dict(os.environ, GIT_SSH_COMMAND=GIT_SSH))


def _watchlist_from(text: str) -> dict:
    try:
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    decision = data.get("last_decision") or {}
    return {
        "ok": True,
        "updated": str(data.get("updated", "?")),
        "count": len(data.get("stocks") or []),
        "decided_at": str(decision.get("generated_at", "")),
        "source_commit": str(decision.get("source_commit", ""))[:12],
        "codes": [s.get("code") for s in (data.get("stocks") or []) if isinstance(s, dict)],
    }


def pool_info() -> dict:
    try:
        pointer = json.loads((SCRIPT_DIR / "pool_batch_latest.json").read_text(encoding="utf-8"))
        batch_id = str(pointer.get("batch_id", ""))
        pool = json.loads((SCRIPT_DIR / "pool_batches" / batch_id / "stock_pool.json").read_text(encoding="utf-8"))
        return {"ok": True, "batch_id": batch_id, "generated_at": str(pool.get("generated_at", "")),
                "market": f"{pool.get('market_status', '?')}{pool.get('market_score', '')}",
                "core": len(pool.get("core_pool") or []), "watch": len(pool.get("watch_pool") or [])}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def local_watchlist() -> dict:
    try:
        return _watchlist_from((SCRIPT_DIR / "watchlist.json").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def probe() -> dict:
    result = {"checked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pool": pool_info()}
    fetch = _git(["fetch", "origin", "main"])
    if fetch.returncode != 0:
        result["error"] = f"git fetch 失败: {(fetch.stderr or '').strip()[:300]}"
        return result
    behind = _git(["rev-list", "--count", "HEAD..origin/main"]).stdout.strip() or "0"
    ahead = _git(["rev-list", "--count", "origin/main..HEAD"]).stdout.strip() or "0"
    changed = _git(["diff", "--name-only", "HEAD", "origin/main"]).stdout.split()
    result["remote"] = {
        "head": _git(["log", "-1", "--format=%h %s", "origin/main"]).stdout.strip(),
        "behind": int(behind), "ahead": int(ahead), "changed": changed,
        "code_changed": [p for p in changed if Path(p).suffix.lower() not in DATA_SUFFIXES],
    }
    shown = _git(["show", "origin/main:watchlist.json"])
    result["remote"]["watchlist"] = (
        _watchlist_from(shown.stdout) if shown.returncode == 0
        else {"ok": False, "error": (shown.stderr or "").strip()[:200]}
    )
    result["local_watchlist"] = local_watchlist()
    result["remote"]["ready"] = _ready(result["remote"]["watchlist"], result["pool"])
    result["local_ready"] = _ready(result["local_watchlist"], result["pool"])
    return result


def adjudication_landed(wl: dict, pool: dict) -> bool:
    """裁决是否属于本轮：裁决时间必须晚于当日池生成时间，且日期是今天。"""
    if not wl.get("ok") or not pool.get("ok"):
        return False
    today = datetime.date.today().isoformat()
    decided = wl.get("decided_at") or ""
    if decided[:10] != today and wl.get("updated") != today:
        return False
    if decided and pool.get("generated_at"):
        return decided >= pool["generated_at"]
    return True  # 无精确时间戳时按日期判断，宁松不误杀


def _ready(wl: dict, pool: dict) -> bool:
    """兼容旧名（内部调用）。"""
    return adjudication_landed(wl, pool)


def report(r: dict) -> None:
    print(f"[裁决检测] 时间={r['checked_at']}")
    if r.get("error"):
        print(f"[裁决检测] ❌ {r['error']}")
        return
    p = r["pool"]
    print(f"[裁决检测] 池: {'✅' if p.get('ok') else '❌'} "
          + (f"批次={p['batch_id']} 生成={p['generated_at']} 市场={p['market']} core={p['core']} watch={p['watch']}"
             if p.get("ok") else p.get("error", "")))
    rm = r["remote"]
    print(f"[裁决检测] 远程: origin/main={rm['head']} 领先本机 {rm['behind']} / 本机领先 {rm['ahead']} 个提交")
    if rm["changed"]:
        print(f"[裁决检测] 远程改动文件({len(rm['changed'])}): {', '.join(rm['changed'][:6])}")
    if rm["code_changed"]:
        print(f"[裁决检测] ⚠️ 远程含代码/配置改动（不会自动合并，需人工复核）: {', '.join(rm['code_changed'][:5])}")
    rw = rm["watchlist"]
    if rw.get("ok"):
        print(f"[裁决检测] 远程裁决: {'✅ 已推送' if rm['ready'] else '⏳ 未到位'} "
              f"updated={rw['updated']} 只数={rw['count']} 裁决时间={rw['decided_at'] or '?'} "
              f"commit={rw['source_commit'] or '-'}")
        if rw["codes"]:
            print(f"[裁决检测] 远程名单: {', '.join(str(c) for c in rw['codes'][:12])}")
    else:
        print(f"[裁决检测] 远程裁决: 读取失败 {rw.get('error')}")
    lw = r["local_watchlist"]
    if lw.get("ok"):
        print(f"[裁决检测] 本机裁决: {'✅ 已同步' if r['local_ready'] else '⏳ 未同步'} "
              f"updated={lw['updated']} 只数={lw['count']} 裁决时间={lw['decided_at'] or '?'}")
    if rm["ready"] and r["local_ready"]:
        print("结论: ✅ 外部裁决已落地并已同步，可以跑技术分析")
    elif rm["ready"]:
        print("结论: ✅ 外部裁决已推送到远程（本机未合并）；直接跑技术分析会自动合并数据文件")
    else:
        print("结论: ⏳ 外部裁决还没推上来（或裁的不是本轮池）；此时跑分析只会用旧名单")


def main() -> int:
    ap = argparse.ArgumentParser(description="探测外部 AI 裁决是否已推送 git")
    ap.add_argument("--wait", type=int, default=0, metavar="分钟", help="轮询等待（每60秒一次，0=只查一次）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    deadline = time.time() + args.wait * 60
    attempt = 0
    while True:
        attempt += 1
        r = probe()
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=1))
        else:
            if attempt > 1:
                print(f"--- 第 {attempt} 次 ---")
            report(r)
        if r.get("error"):
            return 2
        if r["remote"].get("ready"):
            return 0
        if time.time() >= deadline:
            if not args.json and args.wait:
                print(f"[裁决检测] 等待 {args.wait} 分钟仍未检测到裁决推送（退出码 1）")
            return 1
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
