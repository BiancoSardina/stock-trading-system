#!/bin/bash
# ============================================================
# cron 执行记录快照 → git 提交推送
#   - executions.db 一致性快照（sqlite backup API，处理 WAL）
#   - output/ 全部 .md 报告存档同步到仓库 cron_data/output/
#   - git commit + push origin main
# 语义（no_agent watchdog）：
#   成功 = 完全静默（exit 0，无 stdout）
#   失败 = stderr 报错 + 非零退出（cron 自动发错误告警）
# ============================================================
set -uo pipefail

REPO=/home/ubuntu/.hermes/scripts
CRON_DIR=/home/ubuntu/.hermes/cron
SNAP_DIR=$REPO/cron_data

cd "$REPO" || { echo "cd 仓库失败" >&2; exit 1; }

mkdir -p "$SNAP_DIR/output" || { echo "创建 cron_data 目录失败" >&2; exit 1; }

# 1. executions.db 一致性快照（源只读打开，兼容 WAL 模式）
python3 - "$CRON_DIR/executions.db" "$SNAP_DIR/executions.db" <<'EOF' || { echo "executions.db 快照失败" >&2; exit 1; }
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f'file:{src}?mode=ro', uri=True)
d = sqlite3.connect(dst)
try:
    with d:
        s.backup(d)
finally:
    d.close(); s.close()
EOF

# 2. output 报告存档同步
mkdir -p "$SNAP_DIR/output"
rsync -a --delete "$CRON_DIR/output/" "$SNAP_DIR/output/" || { echo "output 同步失败" >&2; exit 1; }

# 3. 提交推送（无变化则静默退出）
git add -A
if git diff --cached --quiet; then
    exit 0
fi
git commit -m "快照：cron执行记录 $(date +%Y%m%d_%H%M)" >/dev/null 2>&1 || { echo "git commit 失败" >&2; exit 1; }
git push origin main >/dev/null 2>&1 || { echo "git push 失败" >&2; exit 1; }
exit 0
