#!/usr/bin/env python3
"""
tech_analysis_bundle.py — 盘中全量技术分析 → 数据包 → git 上传 → QQ 通知（2026-09-07 用户需求）

流程：
  1. pinned_current 钉住当前股票池快照（读批次指针；无指针回落根目录旧池）
  2. subprocess 跑 short_term.py 全量技术分析（ANALYSIS_ONLY=1 不写信号/状态、
     QQ_SEND_DISABLE=1 不直发 QQ），捕获 stdout 报告全文 + 产出 analysis_metrics.json
  3. 按覆盖率给出完整性三态结论（完整 / 部分缺失 / 不可用）并写入数据包
     · 完整      → 正常归档上传
     · 部分缺失  → 正常归档上传，包内明确列出失败标的（失败标的不形成买点结论）
     · 不可用    → 不归档上传，写 analysis_runs/<ts>.json 失败记录并以非 0 退出
  4. 将本轮包固定到 analysis_runs/<ts>/technical_analysis_latest.json，再从该快照归档上传
     （latest 只用于查看，不作为上传或重试的数据源）
     → 归档到 data_packages/technical_analysis_latest_<ts>.json → git push → QQ 提醒
     （该步持跨链公共发布锁 runtime.publish_lock()，与池链共用同一把锁）

任务名按运行时段自动推断：09 点→"早盘技术分析" / 11 点→"收割后技术分析" /
13 点→"午后技术分析" / 14 点→"尾盘技术分析"，也可 --task 手动覆盖。

重试：--retry-upload <ts> 只上传该轮快照（兼容已有历史归档），找不到则拒绝，不重新分析。

⚠️ 与 v3.2 全家族不同：本脚本有 argparse（--help 安全）。cron no_agent 调用无需传参。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from runtime import atomic_json, data_path, publish_lock
from pool_batch import pinned_current
import pool_mode

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ANALYSIS_TIMEOUT = 360  # short_term.py 全量最长秒数（与 decision_bundle.py 默认一致）

SCHEMA = "technical-analysis-bundle/v1"
METRICS_NAME = "analysis_metrics.json"
DEFAULT_MIN_COVERAGE = 0.9  # 可被 ANALYSIS_MIN_COVERAGE 覆盖
TS_PATTERN = re.compile(r"^\d{14}$")
VALID_VERDICTS = ("完整", "部分缺失")


def infer_task() -> str:
    """按运行小时推断任务名（09:35 早盘 / 10:30 盘中观察 / 11:10 收割后 / 13:30 午后 / 14:45 尾盘）"""
    hour = time.localtime().tm_hour
    if hour == 9:
        return "早盘技术分析"
    if hour == 10:
        return "盘中观察技术分析"
    if hour == 11:
        return "收割后技术分析"
    if hour == 13:
        return "午后技术分析"
    if hour == 14:
        return "尾盘技术分析"
    return "盘中技术分析"


def task_for_ts(ts: str) -> str:
    """按归档时间戳的小时推断任务名（重试时用）"""
    try:
        return {9: "早盘技术分析", 10: "盘中观察技术分析", 11: "收割后技术分析",
                13: "午后技术分析", 14: "尾盘技术分析"}.get(int(ts[8:10]), "盘中技术分析")
    except (TypeError, ValueError, IndexError):
        return "盘中技术分析"


def coverage_threshold() -> float:
    try:
        return float(os.environ.get("ANALYSIS_MIN_COVERAGE", DEFAULT_MIN_COVERAGE))
    except (TypeError, ValueError):
        return DEFAULT_MIN_COVERAGE


def completeness(metrics, min_coverage=None) -> dict:
    """分析完整性三态：完整 / 部分缺失 / 不可用。失败标的一律不形成买点结论。

    判定（问题2 的边界）：
      · 完整      = 全部标的分析成功，且市场数据完整
      · 部分缺失  = 有标的失败但覆盖率≥阈值；或市场数据缺失（结论仅供研究，不可执行）
                    —— 其他标的结论仍有效，不整批作废
      · 不可用    = 无可分析标的、覆盖率<阈值（股票级数据大面积失败）、或缺少指标文件
                    —— 拒绝归档，写 analysis_runs 失败记录并非 0 退出
    """
    min_coverage = coverage_threshold() if min_coverage is None else float(min_coverage)
    base = {"targets": 0, "analyzed": 0, "failed": [], "coverage_pct": 0.0,
            "market_data_ok": False, "min_coverage": min_coverage}
    if not isinstance(metrics, dict) or not metrics:
        return dict(base, verdict="不可用",
                    reason=f"缺少 {METRICS_NAME}（short_term.py 未产出完整性指标）")
    market_ok = (metrics.get("market") or {}).get("data_ok") is True
    targets = int(metrics.get("targets") or 0)
    failed = list(metrics.get("failed") or [])
    analyzed = int(metrics.get("analyzed", max(0, targets - len(failed))))
    coverage = (analyzed / targets) if targets else 0.0
    info = {"targets": targets, "analyzed": analyzed, "failed": failed,
            "market_data_ok": market_ok, "market_missing": (metrics.get("market") or {}).get("missing", []),
            "min_coverage": min_coverage,
            "coverage_pct": round(coverage * 100, 1)}
    if targets <= 0:
        return dict(info, verdict="不可用", reason="没有可分析的标的（目标数0）")
    if coverage + 1e-9 < min_coverage:
        return dict(info, verdict="不可用",
                    reason=f"覆盖率{info['coverage_pct']}%<阈值{min_coverage*100:.0f}%（股票级数据大面积失败）")
    notes = []
    if failed:
        notes.append(f"失败{len(failed)}只不形成买点结论")
    if not market_ok:
        notes.append("市场数据缺失(market.data_ok≠True)：市场闸门按未知处理，买点结论仅供研究、不可执行")
        notes.extend(info["market_missing"])
    if not notes:
        return dict(info, verdict="完整", reason=f"全部{targets}只标的分析成功")
    return dict(info, verdict="部分缺失",
                reason=f"覆盖率{info['coverage_pct']}%≥阈值{min_coverage*100:.0f}%：" + "；".join(notes))


def read_metrics() -> dict:
    try:
        with open(data_path(METRICS_NAME), encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError) as exc:
        print(f"[tech_analysis_bundle] ⚠️ 读取 {METRICS_NAME} 失败: {exc}", file=sys.stderr, flush=True)
        return {}


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
    """读当前股票池摘要 + 来源判定（正式池 / 降级研究候选 / 回退自选）；容错返回空 dict。"""
    meta = {}
    try:
        pool = json.load(open(data_path("stock_pool.json"), encoding="utf-8"))
        meta = {
            "date": pool.get("date"),
            "generated_at": pool.get("generated_at"),
            "market_status": pool.get("market_status"),
            "market_score": pool.get("market_score"),
            "data_ok": pool.get("data_ok"),
            "mode": pool.get("mode", pool_mode.MODE_FORMAL),
        }
    except Exception as exc:
        print(f"[tech_analysis_bundle] ⚠️ 读 stock_pool.json 元信息失败: {exc}", file=sys.stderr)
    try:
        meta["provenance"] = pool_mode.pool_provenance()
    except Exception as exc:
        meta["provenance"] = {"provenance": pool_mode.PROVENANCE_FALLBACK, "error": str(exc)}
    return meta


def write_bundle(report: str, diagnostics: str, check: dict, ts: str = "") -> str:
    """先保存本轮独立快照，再更新 latest；上传和重试只消费独立快照。"""
    ts = ts or time.strftime("%Y%m%d%H%M%S")
    if not TS_PATTERN.fullmatch(ts):
        raise ValueError("Analysis timestamp must be 14 digits")
    bundle = {
        "schema": SCHEMA,
        "archive_ts": ts,
        "kind": "盘中全量技术分析（无AI解读，原始数据）",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "analysis_only": True,
        "research_contract": {
            "version": "startup/v1", "budget_independent": True,
            "candidate_paths": ["整理启动型", "超跌企稳型"],
            "price_rr_is_before_costs": True, "research_is_not_order": True,
            "D_and_UNKNOWN_forbid_new_buys": True,
            "quality_classification_is_time_independent": True,
            "buy_state_is_point_in_time": True,
        },
        # 完整性三态：完整 / 部分缺失 / 不可用；failed 中的标的不形成买点结论
        "completeness": check,
        "local_ai_called": False,
        "pool": pool_meta(),
        "report": report,
        "diagnostics": (diagnostics or "")[-4000:],
    }
    snapshot_dir = Path(data_path("analysis_runs")) / ts
    # Fail closed on same-second collisions; never replace an earlier retry source.
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    snapshot = snapshot_dir / "technical_analysis_latest.json"
    atomic_json(snapshot, bundle)
    out = data_path("technical_analysis_latest.json")
    atomic_json(out, bundle)
    size = os.path.getsize(out)
    print(f"[tech_analysis_bundle] 📦 分析数据包已生成 {out} ({size/1024:.1f}KB) "
          f"完整性={check.get('verdict')}", file=sys.stderr, flush=True)
    print(f"[tech_analysis_bundle] 本轮重试标识: {ts}", file=sys.stderr, flush=True)
    return str(snapshot)


def record_run(check: dict, task: str, extra: "dict | None" = None) -> str:
    """失败/留档记录：analysis_runs/<时间戳>.json（不进入 git 归档）"""
    ts = time.strftime("%Y%m%d%H%M%S")
    record = {"schema": "analysis-run/v1", "task": task, "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "verdict": check.get("verdict"), "reason": check.get("reason"),
              "targets": check.get("targets"), "analyzed": check.get("analyzed"),
              "coverage_pct": check.get("coverage_pct"), "market_data_ok": check.get("market_data_ok"),
              "failed": check.get("failed") or []}
    record.update(extra or {})
    path = data_path(os.path.join("analysis_runs", ts + ".json"))
    try:
        atomic_json(path, record)
        print(f"[tech_analysis_bundle] 📝 记录 {path}", file=sys.stderr, flush=True)
    except OSError as exc:
        print(f"[tech_analysis_bundle] ⚠️ 记录失败: {exc}", file=sys.stderr, flush=True)
    return path


def upload(task: str, ts: str) -> int:
    """只上传指定轮次快照；兼容已有历史归档，绝不回退到 latest。"""
    if not TS_PATTERN.fullmatch(ts):
        raise ValueError("Analysis timestamp must be 14 digits")
    snapshot = Path(data_path("analysis_runs")) / ts / "technical_analysis_latest.json"
    archived = Path(SCRIPT_DIR) / "data_packages" / f"technical_analysis_latest_{ts}.json"
    source = snapshot if snapshot.is_file() else archived
    if not source.is_file():
        raise ValueError(f"找不到 {ts} 的原始分析包；拒绝用 latest 代替，请重新分析")
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, "data_package_upload.py"),
           "--task", task, "--files", "technical_analysis_latest.json", "--ts", ts]
    env = dict(os.environ, PUBLISH_LOCK_HELD="1")
    with publish_lock(), tempfile.TemporaryDirectory(prefix="analysis_upload_") as temp:
        # Freeze exact source bytes for the child, including legacy archive retries.
        shutil.copyfile(source, Path(temp) / "technical_analysis_latest.json")
        proc = subprocess.run(cmd + ["--source-dir", temp], cwd=SCRIPT_DIR, env=env, timeout=180)
    return proc.returncode


def main():
    parser = argparse.ArgumentParser(description="盘中全量技术分析→数据包→git上传→QQ通知")
    parser.add_argument("--task", default="", help="任务名（默认按小时推断）")
    parser.add_argument("--skip-upload", action="store_true", help="只生成分析包，不归档上传（测试用）")
    parser.add_argument("--retry-upload", default="",
                        help="只重试指定批次(14位ts)的归档上传，不重新分析")
    args = parser.parse_args()

    if args.retry_upload:
        ts = args.retry_upload.strip()
        if not TS_PATTERN.match(ts):
            print(f"[tech_analysis_bundle] ❌ --retry-upload 需要14位时间戳，收到 {ts!r}", file=sys.stderr)
            sys.exit(2)
        task = args.task or task_for_ts(ts)
        print(f"[tech_analysis_bundle] ♻️ 重试归档上传 task={task} ts={ts}", file=sys.stderr, flush=True)
        try:
            code = upload(task, ts)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f"[tech_analysis_bundle] 重试失败：{exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(code)

    task = args.task or infer_task()
    print(f"[tech_analysis_bundle] 🚀 任务: {task} {time.strftime('%Y-%m-%d %H:%M:%S')}", file=sys.stderr, flush=True)

    with pinned_current():
        report, err = run_analysis()
        if not report.strip():
            print("[tech_analysis_bundle] ❌ short_term.py 输出为空，中止", file=sys.stderr)
            sys.exit(1)
        check = completeness(read_metrics())
        ts = time.strftime("%Y%m%d%H%M%S")
        write_bundle(report, err, check, ts)
        if check["verdict"] not in VALID_VERDICTS:
            record_run(check, task, {"archived": False, "archive_ts": ts})
            print(f"[tech_analysis_bundle] ❌ 分析完整性={check['verdict']}：{check['reason']}；"
                  f"不归档上传（诊断包已生成）", file=sys.stderr, flush=True)
            sys.exit(1)
        print(f"[tech_analysis_bundle] ✅ 分析完整性={check['verdict']}：{check['reason']}",
              file=sys.stderr, flush=True)

    if args.skip_upload:
        print("[tech_analysis_bundle] skip-upload：未归档上传", file=sys.stderr)
        return

    try:
        code = upload(task, ts)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        record_run(check, task, {"archived": False, "archive_ts": ts, "error": str(exc)})
        print(f"[tech_analysis_bundle] 上传失败：{exc}；可重试 --retry-upload {ts}", file=sys.stderr)
        sys.exit(1)
    record_run(check, task, {"archived": code == 0, "archive_ts": ts, "upload_exit": code})
    if code:
        print(f"[tech_analysis_bundle] 上传未完成；可重试 --retry-upload {ts}", file=sys.stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
