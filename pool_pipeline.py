"""Budgeted, observable pool publication; never writes orders or watchlists."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

import pool_batch
import pool_mode
import runtime

SCRIPT_DIR = Path(__file__).resolve().parent


@contextmanager
def pipeline_lock():
    """池链整轮发布锁：与技术分析链、归档脚本共用同一把 OS 锁（runtime.publish_lock）。

    子进程通过 PUBLISH_LOCK_HELD=1 放行，避免 upload 步自锁。
    """
    with runtime.publish_lock():
        yield


def terminate_tree(proc):
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=10, check=False)
        finally:
            if proc.poll() is None:
                proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def run_process(command, env, timeout, stdout=None, stderr=None):
    if timeout <= 0:
        raise TimeoutError("No remaining step budget")
    proc = subprocess.Popen(command, cwd=SCRIPT_DIR, env=env, stdout=stdout, stderr=stderr,
                            start_new_session=(os.name != "nt"))
    try:
        code = proc.wait(timeout=timeout)
    except BaseException:
        terminate_tree(proc)
        raise
    if code:
        terminate_tree(proc)
        raise RuntimeError(f"{Path(command[1]).name} failed, exit={code}")


def notify_failure(task, batch_id, stage, error, run_id):
    message = f"❌ {task}流程未全部完成\n批次: {batch_id}\n阶段: {stage}\n原因: {error}\nGit上传状态与重试信息见 pipeline_runs/{run_id}.json 和日志。"
    print(message, flush=True)
    import qq_send
    from data_package_upload import DEFAULT_QQ_OPENID
    return qq_send.send_report(message, openid=os.environ.get("QQ_TARGET_OPENID") or DEFAULT_QQ_OPENID)


class Pipeline:
    def __init__(self, task, retry_batch=None):
        self.task = task
        self.batch_id = retry_batch or datetime.now().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:8]
        self.directory = pool_batch.batch_dir(self.batch_id)
        self.retry = bool(retry_batch)
        self.started = time.monotonic()
        total = float(os.environ.get("PIPELINE_TIMEOUT", "900"))
        self.bundle_reserve = float(os.environ.get("PIPELINE_BUNDLE_BUDGET", "120"))
        self.upload_reserve = float(os.environ.get("PIPELINE_UPLOAD_BUDGET", "120"))
        self.notify_reserve = float(os.environ.get("PIPELINE_NOTIFY_BUDGET", "20"))
        values = (total, self.bundle_reserve, self.upload_reserve, self.notify_reserve)
        if any(not math.isfinite(v) or v <= 0 for v in values) or total <= sum(values[1:]):
            raise ValueError("Pipeline budgets must be positive and leave time for scanning")
        self.deadline = self.started + total
        self.work_deadline = self.deadline - self.notify_reserve
        self.record = {"schema": "pool-pipeline-run/v1", "batch_id": self.batch_id, "task": task,
                       "started_at": datetime.now().isoformat(), "status": "running", "stage": "prepare",
                       "total_budget_seconds": total, "retry": self.retry, "steps": []}
        self.run_id = self.batch_id + ("_retry_" + uuid.uuid4().hex[:8] if self.retry else "")
        self.record["run_id"] = self.run_id
        self.record_path = runtime.DATA_DIR / "pipeline_runs" / (self.run_id + ".json")
        self.env = dict(os.environ, POOL_BATCH_DIR=str(self.directory.resolve()),
                        POOL_BATCH_ID=self.batch_id, PYTHONUTF8="1", PIPELINE_COMPACT="1",
                        PUBLISH_LOCK_HELD="1")  # 子进程不再重复获取父流程已持有的发布锁

    def save(self):
        self.record["elapsed_seconds"] = round(time.monotonic() - self.started, 2)
        self.record["remaining_seconds"] = round(max(0, self.deadline - time.monotonic()), 2)
        runtime.atomic_json(self.record_path, self.record)

    def step(self, name, script, deadline, args=(), extra=None):
        self.record["stage"] = name
        event = {"name": name, "status": "running", "started_at": datetime.now().isoformat()}
        self.record["steps"].append(event)
        self.save()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            event["status"] = "failed"
            raise TimeoutError(f"{name}: reserved budget exhausted")
        env = dict(self.env, **(extra or {}))
        env["PIPELINE_STEP_DEADLINE"] = str(deadline)
        start = time.monotonic()
        log_path = self.record_path.with_suffix(".log")
        print(f"[pool_pipeline] {name}: 可用{remaining:.0f}s；日志 {log_path}", file=sys.stderr, flush=True)
        try:
            with log_path.open("ab", buffering=0) as log:
                run_process([sys.executable, str(SCRIPT_DIR / script), *args], env, remaining, log, log)
            event["status"] = "completed"
        except BaseException:
            event["status"] = "failed"
            raise
        finally:
            event["seconds"] = round(time.monotonic() - start, 2)
            self.save()

    def _research_payload(self):
        """批次目录里的降级研究候选（存在即本轮是降级研究轮）。"""
        try:
            return runtime.read_json(self.directory / pool_mode.RESEARCH_FILE, {}) or None
        except (ValueError, OSError):
            return None

    def execute(self):
        try:
            with pipeline_lock():
                self.save()
                if not self.retry:
                    self.directory.mkdir(parents=True, exist_ok=False)
                    old_pool = pool_batch.current_directory() / "stock_pool.json"
                    if old_pool.exists():
                        shutil.copy2(old_pool, self.directory / "stock_pool.json")
                    self.step("pool", "stock_pool.py", self.work_deadline - self.bundle_reserve - self.upload_reserve)
                    research = self._research_payload()
                    if research is None:
                        self.step("bundle", "decision_bundle.py", self.work_deadline - self.upload_reserve,
                                  extra={"ANALYSIS_ONLY": "1", "BUNDLE_RUN_ANALYSIS": "1"})
                        self.record["stage"] = "validate_publish"
                        self.save()
                        if time.monotonic() >= self.work_deadline - self.upload_reserve:
                            raise TimeoutError("Insufficient time to validate/publish and upload")
                        pool_batch.publish(self.batch_id)
                        self.record["pool_mode"] = pool_mode.MODE_FORMAL
                        self.record["complete_batch"] = True
                    else:
                        # 降级研究轮：不动正式池指针 / 不生成裁决包 / 不写 watchlist
                        pool_mode.validate_research_payload(research)
                        self.record["pool_mode"] = pool_mode.MODE_DEGRADED
                        self.record["degraded_reasons"] = research.get("degraded_reasons") or []
                        self.record["stage"] = "degraded_research"
                        print(f"[pool_pipeline] ⚠️ 降级研究轮（{'；'.join(self.record['degraded_reasons'])}）；"
                              f"正式池指针保持不动", file=sys.stderr, flush=True)
                else:
                    research = self._research_payload()
                    if research is not None:
                        pool_mode.validate_research_payload(research)
                        self.record["pool_mode"] = pool_mode.MODE_DEGRADED
                        self.record["degraded_reasons"] = research.get("degraded_reasons") or []
                    else:
                        ready = runtime.read_json(self.directory / "ready.json", {})
                        if ready.get("batch_id") != self.batch_id:
                            raise ValueError("Retry requires a complete, validated batch")
                        pool_batch.validate_pair(self.directory)
                        for name in pool_batch.NAMES:
                            if ready.get("sha256", {}).get(name) != pool_batch.digest(self.directory / name):
                                raise ValueError("Retry batch changed")
                        self.record["pool_mode"] = pool_mode.MODE_FORMAL
                        self.record["complete_batch"] = True
                self.save()
                degraded = self.record.get("pool_mode") == pool_mode.MODE_DEGRADED
                task = f"{self.task}·降级研究" if degraded else self.task
                extra_args = ("--files", pool_mode.RESEARCH_FILE) if degraded else ()
                self.step("upload", "data_package_upload.py", self.work_deadline,
                          args=("--task", task, *extra_args, "--source-dir", str(self.directory.resolve()),
                                "--receipt", str(self.record_path.with_suffix(".upload.json"))))
                self.record.update(status="completed", stage="degraded_done" if degraded else "done")
                self.save()
                if degraded:
                    print(f"[pool_pipeline] ⚠️ {self.task}降级研究候选完成 batch={self.batch_id}"
                          f"（正式池未更新）", flush=True)
                else:
                    print(f"[pool_pipeline] ✅ {self.task}完成 batch={self.batch_id}", flush=True)
                return 0
        except Exception as exc:
            self.record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            try:
                self.record["upload_receipt"] = runtime.read_json(self.record_path.with_suffix(".upload.json"), {})
            except (ValueError, OSError):
                self.record["upload_receipt"] = {"status": "unreadable; verify remote before retry"}
            self.record["batch_locks"] = [p.name for p in self.directory.glob("*.lock")]
            try:
                self.save()
            except OSError as save_exc:
                print(f"[pool_pipeline] 无法保存失败记录: {save_exc}", flush=True)
            print(f"[pool_pipeline] ❌ {self.record['error']}；记录 {self.record_path}", flush=True)
            try:
                run_process([sys.executable, str(Path(__file__).resolve()), "--notify", self.task,
                             self.batch_id, self.record["stage"], self.record["error"], self.run_id],
                            dict(os.environ), min(self.notify_reserve, max(1, self.deadline-time.monotonic())))
                self.record["failure_notification"] = "sent"
            except Exception as notify_exc:
                self.record["failure_notification"] = f"failed: {type(notify_exc).__name__}"
                print("[pool_pipeline] 失败通知未送达；请检查本地失败记录/Hermes任务日志", flush=True)
            try:
                self.save()
            except OSError:
                pass  # Notification/stdout must still be attempted when the disk is full.
            return 1


def main(task):
    parser = argparse.ArgumentParser(description="同批次股票池与裁决包完整发布")
    parser.add_argument("--retry-batch", help="重试当天完整批次的归档/上传，不重新扫描或回退当前批次")
    args = parser.parse_args()
    return Pipeline(task, args.retry_batch).execute()


if __name__ == "__main__":
    if len(sys.argv) == 7 and sys.argv[1] == "--notify":
        sys.exit(0 if notify_failure(*sys.argv[2:]) else 1)
    sys.exit(main("股票池"))
