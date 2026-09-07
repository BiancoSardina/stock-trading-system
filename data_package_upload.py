#!/usr/bin/env python3
"""
data_package_upload.py — 股票池数据包归档 + git 上传 + QQ 提醒（2026-09-07 用户需求）

新逻辑：每次股票池数据包生成后，
  1) 把最新数据包（stock_pool.json + decision_bundle_latest.json）复制到
     data_packages/ 目录，文件名带时间戳 yyyymmddHHMMSS（例：stock_pool_20260907180630.json）
  2) git add data_packages/ + commit + push origin main（只动 data_packages/ 目录，
     不碰工作区其他被 cron 实时改写的数据文件）
  3) push 成功后给 QQ 发消息提醒（"XX 数据包上传完成"）

用法：
  python3 data_package_upload.py                       # 归档当前最新数据包（任务名默认"股票池任务"）
  python3 data_package_upload.py --task 收盘生成        # 自定义任务名（进 commit 消息与 QQ 文案）
  python3 data_package_upload.py --dry-run             # 只归档到 data_packages/，不 git 不 QQ

注意：本脚本与 stock_pool_full.py / stock_pool_evening.py 无 argparse 陷阱不同——
本脚本有 argparse，--help 安全。
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_DIR = os.path.join(SCRIPT_DIR, "data_packages")

# 要归档的数据包源文件（相对 SCRIPT_DIR）
SOURCE_FILES = [
    "stock_pool.json",
    "decision_bundle_latest.json",
]

# QQ 接收人 openid：与 cron 任务 deliver=qqbot:4280B621120ABAD4D7E837F57EF66187 一致。
# qq_send.py 的 DEFAULT_OPENID 只读环境变量，这里兜底注入（本脚本只发给用户本人）。
DEFAULT_QQ_OPENID = "4280B621120ABAD4D7E837F57EF66187"


def ts_now() -> str:
    return time.strftime("%Y%m%d%H%M%S")


def archive(task: str, dry_run: bool = False, files: "list | None" = None):
    """把数据包文件复制到 data_packages/ 带时间戳，返回 (ts, [(归档文件名, 源文件名, 大小)])"""
    os.makedirs(PACKAGE_DIR, exist_ok=True)
    ts = ts_now()
    archived = []
    missing = []
    for name in (files or SOURCE_FILES):
        src = os.path.join(SCRIPT_DIR, name)
        if not os.path.isfile(src):
            missing.append(name)
            continue
        dst_name = f"{os.path.splitext(name)[0]}_{ts}.json"
        dst = os.path.join(PACKAGE_DIR, dst_name)
        if not dry_run:
            shutil.copy2(src, dst)
        size = os.path.getsize(src)
        archived.append((dst_name, name, size))
    if missing:
        print(f"[upload] ⚠️ 缺失源文件: {', '.join(missing)}（继续处理其余）", file=sys.stderr)
    if not archived:
        print("[upload] ❌ 没有任何数据包可归档", file=sys.stderr)
        sys.exit(1)
    return ts, archived


def git_push(task: str, ts: str) -> str:
    """git add data_packages/ → commit → push origin main。返回 commit 短 hash；push 失败抛异常。"""
    def _run(cmd, check=True):
        proc = subprocess.run(cmd, cwd=SCRIPT_DIR, capture_output=True, text=True, timeout=120)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git 命令失败: {' '.join(cmd)}\n{proc.stderr.strip()[:500]}")
        return proc

    _run(["git", "add", "--", "data_packages/"])
    commit_msg = f"数据包归档 {task} {ts}"
    proc = _run(["git", "commit", "-m", commit_msg])
    if "nothing to commit" in proc.stdout or "nothing to commit" in proc.stderr:
        print("[upload] ⚠️ 无新增文件，跳过 push（归档目录可能已被提交过）", file=sys.stderr)
        return ""
    # 取 commit 短 hash
    proc = _run(["git", "rev-parse", "--short", "HEAD"])
    short_hash = proc.stdout.strip()
    # push 到远程（认证失败/无写权限会在这里抛错）
    proc = _run(["git", "push", "origin", "main"])
    print(f"[upload] ✅ git push 成功 commit={short_hash}", file=sys.stderr)
    return short_hash


def qq_notify(task: str, ts: str, archived, commit_hash: str) -> bool:
    """发 QQ 提醒；成功返回 True，失败返回 False（消息原文 print 到 stdout 供兜底）。"""
    lines = [
        f"✅ {task}数据包上传完成",
        f"时间: {ts}",
        "文件:",
    ]
    for dst_name, src_name, size in archived:
        lines.append(f"  {dst_name} ({size/1024:.1f}KB)")
    if commit_hash:
        lines.append(f"commit: {commit_hash}")
    lines.append("已推送 git 远程 (origin/main)")
    text = "\n".join(lines)

    sys.path.insert(0, SCRIPT_DIR)
    try:
        import qq_send
    except Exception as exc:
        print(text, file=sys.stdout)
        print(f"[upload] ❌ 无法加载 qq_send: {exc}", file=sys.stderr)
        return False

    # ⚠️ 必须显式传 openid 参数：qq_send 模块 import 时 DEFAULT_OPENID 已从环境变量定格，
    # 之后 setdefault 环境变量不生效（2026-09-07 实测踩坑）
    try:
        ok = qq_send.send_report(text, openid=DEFAULT_QQ_OPENID)
    except Exception as exc:
        print(text, file=sys.stdout)
        print(f"[upload] ❌ QQ 发送失败: {exc}", file=sys.stderr)
        return False
    if not ok:
        print(text, file=sys.stdout)
        print("[upload] ⚠️ QQ 发送未成功（原文已输出到 stdout）", file=sys.stderr)
    return ok


def main():
    parser = argparse.ArgumentParser(description="股票池数据包归档 + git 上传 + QQ 提醒")
    parser.add_argument("--task", default="股票池任务", help="任务名（进 commit 消息与 QQ 文案）")
    parser.add_argument("--dry-run", action="store_true", help="只归档到 data_packages/，不 git 不 QQ")
    parser.add_argument("--skip-qq", action="store_true", help="git 上传但跳过 QQ 提醒")
    parser.add_argument("--files", default="",
                        help="逗号分隔的源文件名，覆盖默认(stock_pool.json,decision_bundle_latest.json)")
    args = parser.parse_args()
    files = [f.strip() for f in args.files.split(",") if f.strip()] or None

    ts, archived = archive(args.task, dry_run=args.dry_run, files=files)
    names = ", ".join(d[0] for d in archived)
    print(f"[upload] 📦 归档完成 ts={ts}: {names}", file=sys.stderr)

    if args.dry_run:
        print("[upload] dry-run：未执行 git / QQ", file=sys.stderr)
        return

    commit_hash = ""
    try:
        commit_hash = git_push(args.task, ts)
    except RuntimeError as exc:
        print(f"[upload] ❌ git 上传失败: {exc}", file=sys.stderr)
        print(f"[upload] 本地归档已保留在 {PACKAGE_DIR}，可修复后重跑", file=sys.stderr)
        sys.exit(1)

    if args.skip_qq:
        return

    ok = qq_notify(args.task, ts, archived, commit_hash)
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
