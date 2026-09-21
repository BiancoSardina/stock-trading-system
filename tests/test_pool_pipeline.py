"""Offline failure injection; no market, QQ, git push or production data access."""
from contextlib import ExitStack
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import daily_history
import data_package_upload as upload
import decision_bundle
import pool_batch
import pool_pipeline as pipeline
import runtime
import stock_pool


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(runtime, "DATA_DIR", self.root))
        self.stack.enter_context(patch.object(upload, "DATA_DIR", self.root))
        self.stack.enter_context(patch.object(upload, "PACKAGE_DIR", str(self.root / "archives")))
        env = dict(os.environ)
        for key in ("POOL_BATCH_DIR", "POOL_BATCH_ID", "PIPELINE_STEP_DEADLINE", "PIPELINE_TIMEOUT",
                    "PIPELINE_BUNDLE_BUDGET", "PIPELINE_UPLOAD_BUDGET", "PIPELINE_NOTIFY_BUDGET"):
            env.pop(key, None)
        self.stack.enter_context(patch.dict(os.environ, env, clear=True))
        self.now = datetime.now().replace(microsecond=0)
        self.old_id = self.now.strftime("%Y%m%d%H%M%S") + "_11111111"
        self.new_id = self.now.strftime("%Y%m%d%H%M%S") + "_22222222"

    def pair(self, batch_id=None, directory=None):
        batch_id = batch_id or self.new_id
        directory = Path(directory) if directory else pool_batch.batch_dir(batch_id)
        directory.mkdir(parents=True, exist_ok=True)
        generated = self.now - timedelta(seconds=2)
        pool = {"batch_id": batch_id, "date": self.now.strftime("%Y-%m-%d"),
                "generated_at": generated.strftime("%Y-%m-%d %H:%M:%S"), "data_ok": True,
                "market_status": "B", "market_score": 70, "core_pool": [], "watch_pool": [{"code": "600577"}],
                "scan_metrics": {"complete": True, "candidate_count": 1, "processed": 1}}
        bundle = decision_bundle.build_bundle(pool, {}, {}, {"report": "test"}, self.now)
        runtime.atomic_json(directory / pool_batch.NAMES[0], pool)
        runtime.atomic_json(directory / pool_batch.NAMES[1], bundle)
        return directory, pool, bundle

    def old_pointer(self):
        self.pair(self.old_id)
        pool_batch.publish(self.old_id, self.now)
        return (self.root / pool_batch.POINTER).read_bytes()

    def test_publish_switches_one_pointer_to_matching_pair(self):
        self.old_pointer()
        directory, _, _ = self.pair()
        pool_batch.publish(self.new_id, self.now)
        for name in pool_batch.NAMES:
            self.assertEqual(Path(runtime.data_path(name)), directory / name)

    def test_missing_bundle_keeps_old_pointer(self):
        previous = self.old_pointer()
        directory, _, _ = self.pair()
        (directory / pool_batch.NAMES[1]).unlink()
        with self.assertRaises(FileNotFoundError):
            pool_batch.publish(self.new_id, self.now)
        self.assertEqual(previous, (self.root / pool_batch.POINTER).read_bytes())

    def test_mixed_constituents_generation_and_unknown_market_rejected(self):
        previous = self.old_pointer()
        for kind in ("contents", "timestamp", "unknown", "id", "schema", "integrity", "expired", "partial"):
            directory, pool, bundle = self.pair()
            if kind == "contents": bundle["stock_pool"]["watch_pool"] = []
            if kind == "timestamp": bundle["market"]["generated_at"] = "2000-01-01 00:00:00"
            if kind == "unknown": pool["market_status"] = bundle["market"]["market_status"] = "UNKNOWN"
            if kind == "id": bundle["batch_id"] = self.old_id
            if kind == "schema": bundle["schema"] = "wrong"
            if kind == "integrity": bundle["integrity"]["pool_data_ok"] = False
            if kind == "expired": bundle["valid_until"] = "2000-01-01 00:00:00"
            if kind == "partial": pool["scan_metrics"]["processed"] = 0
            runtime.atomic_json(directory / pool_batch.NAMES[0], pool)
            runtime.atomic_json(directory / pool_batch.NAMES[1], bundle)
            with self.assertRaises(ValueError, msg=kind):
                pool_batch.publish(self.new_id, self.now)
            self.assertEqual(previous, (self.root / pool_batch.POINTER).read_bytes())

    def test_corrupt_manifest_never_falls_back_to_legacy(self):
        self.old_pointer()
        runtime.atomic_json(self.root / "stock_pool.json", {"old": True})
        runtime.atomic_json(pool_batch.batch_dir(self.old_id) / "stock_pool.json", {"changed": True})
        with self.assertRaises(ValueError):
            runtime.data_path("stock_pool.json")

    def test_pinned_consumer_stays_on_same_batch_during_switch(self):
        self.old_pointer()
        self.pair()
        with pool_batch.pinned_current():
            pool_batch.publish(self.new_id, self.now)
            self.assertEqual(Path(runtime.data_path("stock_pool.json")).parent.name, self.old_id)
        self.assertEqual(Path(runtime.data_path("stock_pool.json")).parent.name, self.new_id)

    def test_published_files_cannot_be_rewritten_by_standalone_writer(self):
        self.old_pointer()
        with self.assertRaises(RuntimeError): pool_batch.write_path("stock_pool.json")
        with pool_batch.pinned_current():
            with self.assertRaises(RuntimeError): pool_batch.write_path("decision_bundle_latest.json")

    def fake_runner(self, fail=None):
        calls = []
        def run(command, env, timeout, *streams):
            self.assertGreater(timeout, 0)
            if "--notify" in command:
                calls.append("notify")
                return
            script = Path(command[1]).name
            calls.append(script)
            if script == fail:
                raise subprocess.TimeoutExpired(command, timeout)
            if script == "stock_pool.py":
                self.pair(env["POOL_BATCH_ID"], env["POOL_BATCH_DIR"])
                (Path(env["POOL_BATCH_DIR"]) / "decision_bundle_latest.json").unlink()
            elif script == "decision_bundle.py":
                self.pair(env["POOL_BATCH_ID"], env["POOL_BATCH_DIR"])
        return run, calls

    def test_pool_or_bundle_timeout_preserves_old_batch_and_notifies(self):
        for script in ("stock_pool.py", "decision_bundle.py"):
            previous = self.old_pointer()
            run, calls = self.fake_runner(script)
            p = pipeline.Pipeline("test")
            with patch.object(pipeline, "run_process", side_effect=run):
                self.assertEqual(p.execute(), 1)
            self.assertEqual(previous, (self.root / pool_batch.POINTER).read_bytes())
            self.assertNotIn("data_package_upload.py", calls)
            self.assertEqual(calls[-1], "notify")
            self.assertEqual(p.record["status"], "failed")

    def test_upload_timeout_keeps_complete_retryable_batch(self):
        self.old_pointer()
        run, calls = self.fake_runner("data_package_upload.py")
        p = pipeline.Pipeline("test")
        with patch.object(pipeline, "run_process", side_effect=run):
            self.assertEqual(p.execute(), 1)
        self.assertEqual(pool_batch.current_directory(), p.directory)
        self.assertTrue(p.record["complete_batch"])
        self.assertEqual(calls[-1], "notify")

    def test_success_never_sends_failure_notice(self):
        run, calls = self.fake_runner()
        p = pipeline.Pipeline("test")
        with patch.object(pipeline, "run_process", side_effect=run):
            self.assertEqual(p.execute(), 0)
        self.assertNotIn("notify", calls)
        self.assertEqual(p.record["status"], "completed")

    def test_retry_does_not_scan_or_roll_pointer_back(self):
        self.old_pointer()
        self.pair()
        pool_batch.publish(self.new_id, self.now)
        run, calls = self.fake_runner()
        p = pipeline.Pipeline("test", self.old_id)
        with patch.object(pipeline, "run_process", side_effect=run):
            self.assertEqual(p.execute(), 0)
        self.assertEqual(calls, ["data_package_upload.py"])
        self.assertEqual(pool_batch.current_directory().name, self.new_id)

    def test_retry_preserves_original_failure_record(self):
        self.old_pointer()
        original = self.root / "pipeline_runs" / (self.old_id + ".json")
        runtime.atomic_json(original, {"status": "failed", "reason": "original"})
        run, _ = self.fake_runner()
        p = pipeline.Pipeline("test", self.old_id)
        with patch.object(pipeline, "run_process", side_effect=run):
            self.assertEqual(p.execute(), 0)
        self.assertNotEqual(p.record_path, original)
        self.assertEqual(runtime.read_json(original, {})["reason"], "original")

    def test_failure_notice_error_is_recorded_without_masking_original(self):
        self.old_pointer()
        p = pipeline.Pipeline("test")
        with patch.object(pipeline, "run_process", side_effect=RuntimeError("offline fault")):
            self.assertEqual(p.execute(), 1)
        self.assertEqual(p.record["failure_notification"], "failed: RuntimeError")
        self.assertEqual(p.record["stage"], "pool")

    def test_qq_failure_keeps_git_success_receipt(self):
        run, _ = self.fake_runner()
        p = pipeline.Pipeline("test")
        def qq_failed(command, env, timeout, *streams):
            if Path(command[1]).name == "data_package_upload.py":
                receipt_path = command[command.index("--receipt") + 1]
                runtime.atomic_json(receipt_path, {"git_pushed": True, "qq_sent": False, "commit": "abc123"})
                raise RuntimeError("QQ send failed after successful push")
            return run(command, env, timeout, *streams)
        with patch.object(pipeline, "run_process", side_effect=qq_failed):
            self.assertEqual(p.execute(), 1)
        self.assertTrue(p.record["upload_receipt"]["git_pushed"])
        self.assertFalse(p.record["upload_receipt"]["qq_sent"])

    def test_os_lock_blocks_overlap_and_releases_after_exit(self):
        with pipeline.pipeline_lock():
            with self.assertRaises(OSError):
                with pipeline.pipeline_lock():
                    self.fail("Overlapping run acquired lock")
        with pipeline.pipeline_lock():
            pass

    def test_stage_budget_reserves_bundle_upload_and_notification(self):
        p = pipeline.Pipeline("test")
        scan_budget = p.work_deadline - p.bundle_reserve - p.upload_reserve - p.started
        self.assertEqual(scan_budget, 640)
        with patch.object(pipeline, "run_process") as run:
            with self.assertRaises(TimeoutError): p.step("pool", "stock_pool.py", 0)
            run.assert_not_called()

    def test_child_timeout_terminates_tree(self):
        proc = Mock()
        proc.wait.side_effect = subprocess.TimeoutExpired("test", 1)
        with patch.object(subprocess, "Popen", return_value=proc), patch.object(pipeline, "terminate_tree") as stop:
            with self.assertRaises(subprocess.TimeoutExpired):
                pipeline.run_process([sys.executable, "test.py"], {}, 1)
            stop.assert_called_once_with(proc)

    def test_nonzero_child_exit_also_cleans_nested_children(self):
        proc = Mock()
        proc.wait.return_value = 1
        with patch.object(subprocess, "Popen", return_value=proc), patch.object(pipeline, "terminate_tree") as stop:
            with self.assertRaises(RuntimeError): pipeline.run_process([sys.executable, "test.py"], {}, 1)
            stop.assert_called_once_with(proc)

    def test_real_short_lived_child_timeout_cleanup(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            pipeline.run_process([sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ), .05)

    def test_batch_locks_are_isolated_not_deleted_globally(self):
        old = runtime.data_path("short_term.run")
        with patch.dict(os.environ, POOL_BATCH_DIR=str(self.root / "batch")):
            self.assertEqual(runtime.data_path("short_term.run"), str(self.root / "batch" / "short_term.run"))
            self.assertNotEqual(runtime.data_path("short_term.run"), old)

    def test_missing_upload_input_copies_nothing(self):
        directory, _, _ = self.pair()
        (directory / "decision_bundle_latest.json").unlink()
        with self.assertRaises(ValueError): upload.archive("test", source_dir=directory)
        self.assertFalse((self.root / "archives").exists())

    def test_archive_retry_has_same_names_and_preserves_bytes(self):
        directory, _, _ = self.pair()
        pool_batch.publish(self.new_id, self.now)
        first = upload.archive("test", source_dir=directory)
        second = upload.archive("test", source_dir=directory)
        self.assertEqual(first, second)
        for dst, src, _ in first[1]:
            self.assertEqual((self.root / "archives" / dst).read_bytes(), (directory / src).read_bytes())

    def test_archive_dry_run_does_not_create_files(self):
        directory, _, _ = self.pair()
        pool_batch.publish(self.new_id, self.now)
        upload.archive("test", dry_run=True, source_dir=directory)
        self.assertFalse((self.root / "archives").exists())

    def test_no_diff_upload_retries_push_and_stages_exact_files(self):
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            output = "main\n" if "--show-current" in command else "abc123\n" if "rev-parse" in command else ""
            return subprocess.CompletedProcess(command, 0, output, "")
        with patch.object(subprocess, "run", side_effect=run):
            upload.git_push("test", "batch", [("pool.json", "source", 10)])
        self.assertIn(["git", "push", "origin", "main"], commands)
        self.assertIn(["git", "add", "--", "data_packages/pool.json"], commands)
        self.assertFalse(any("commit" in c for c in commands))

    def test_unrelated_staged_changes_abort_upload(self):
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "watchlist.json\n", "")) as run:
            with self.assertRaises(RuntimeError): upload.git_push("test", "batch", [("pool.json", "source", 10)])
            self.assertEqual(run.call_count, 1)

    def test_actual_pool_entry_stops_on_bulk_feed_failure_without_saving(self):
        previous = self.old_pointer()
        candidates = [{"code": f"600{i:03d}", "name": "测试", "amount": 1e9} for i in range(30)]
        directory = pool_batch.batch_dir(self.new_id)
        directory.mkdir()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, POOL_BATCH_DIR=str(directory), POOL_BATCH_ID=self.new_id))
            stack.enter_context(patch.object(sys, "argv", ["stock_pool.py"]))
            stack.enter_context(patch.object(stock_pool.short_term, "market_score"))
            stack.enter_context(patch.object(stock_pool.short_term, "MARKET", {"state": "B", "score": 70, "data_ok": True}))
            stack.enter_context(patch.object(stock_pool.stock_scanner, "fetch_all_stocks", return_value=candidates))
            stack.enter_context(patch.object(stock_pool.stock_scanner, "LAST_FETCH_COMPLETE", True))
            stack.enter_context(patch.object(stock_pool.stock_scanner, "basic_filter", return_value=(candidates, {})))
            stack.enter_context(patch.object(stock_pool.industry_rank, "build_industry_map", return_value={}))
            stack.enter_context(patch.object(stock_pool.industry_rank, "score_industries", return_value={"行业": {"score": 60}}))
            stack.enter_context(patch.object(stock_pool.industry_rank, "stock_industry_index", return_value={c["code"]: "行业" for c in candidates}))
            stack.enter_context(patch.object(stock_pool.short_term, "get_index_kline", return_value=[{"day": "2026-09-17", "close": 10}] * 30))
            scorer = stack.enter_context(patch.object(stock_pool, "score_stock", return_value=None))
            save = stack.enter_context(patch.object(stock_pool.spm, "save_pool"))
            stack.enter_context(patch.object(stock_pool, "KLINE_SLEEP", 0))
            with self.assertRaisesRegex(RuntimeError, "候选行情失败过多"):
                stock_pool.main()
            self.assertEqual(scorer.call_count, 16)
            save.assert_not_called()
        self.assertEqual(previous, (self.root / pool_batch.POINTER).read_bytes())
        metrics = runtime.read_json(directory / "scan_metrics.json", {})
        self.assertEqual(metrics["processed"], 16)
        self.assertFalse(metrics["complete"])

    def history(self):
        return [{"day": (self.now-timedelta(days=75-i)).strftime("%Y-%m-%d"),
                 "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1000} for i in range(75)]

    def test_history_cache_never_caches_intraday_bars_or_quotes(self):
        rows = self.history()
        today = self.now.strftime("%Y-%m-%d")
        rows.append({"day": today, "open": 10, "high": 12, "low": 9, "close": 11, "volume": 3000})
        q = {"date": today, "prev": 10, "cur": 11}
        loader = Mock(return_value=rows)
        daily_history.get("600577", 120, q, loader, self.now)
        cached = daily_history.get("600577", 120, dict(q, cur=12), loader, self.now)
        self.assertEqual(loader.call_count, 1)
        self.assertTrue(all(b["day"] < today for b in cached))
        # A different prior-close basis invalidates a same-day cache hit.
        daily_history.get("600577", 120, dict(q, prev=9), loader, self.now)
        self.assertEqual(loader.call_count, 2)

    def test_cache_never_relabels_weekend_quote_as_today(self):
        q = {"date": (self.now-timedelta(days=1)).strftime("%Y-%m-%d"), "prev": 10}
        loader = Mock(return_value=self.history())
        for _ in range(2): daily_history.get("600577", 120, q, loader, self.now)
        self.assertEqual(loader.call_count, 2)
        self.assertFalse((self.root / "daily_history").exists())

    def test_new_day_and_corrupt_cache_force_refetch(self):
        q = {"date": self.now.strftime("%Y-%m-%d"), "prev": 10}
        loader = Mock(return_value=self.history())
        daily_history.get("600577", 120, q, loader, self.now)
        cache = next((self.root / "daily_history").rglob("*.json"))
        cache.write_text("{broken", encoding="utf-8")
        daily_history.get("600577", 120, q, loader, self.now)
        tomorrow = self.now + timedelta(days=1)
        daily_history.get("600577", 120, dict(q, date=tomorrow.strftime("%Y-%m-%d")), loader, tomorrow)
        self.assertEqual(loader.call_count, 3)


if __name__ == "__main__":
    unittest.main()
