"""分析完整性三态 + 归档门禁 + 暂存残留自愈 + 跨链发布锁 的离线契约。

背景（2026-09-22 用户指出）：
  ① 技术分析只检查"退出码0 + 报告非空"就打包，部分标的失败也会被当成有效结论；
  ② 池链与技术分析链共用同一个 git 工作区，却没有共同的发布锁。
"""
import os
import sys
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime
import tech_analysis_bundle as tab
import data_package_upload as upload
import short_term as st


def metrics(targets=10, failed=0, data_ok=True):
    return {"schema": "analysis-metrics/v1", "targets": targets, "analyzed": targets - failed,
            "failed": [{"code": f"60000{i}", "name": f"失败{i}", "reason": "K线失败"} for i in range(failed)],
            "market": {"state": "B", "score": 70, "data_ok": data_ok}}


class CompletenessTests(unittest.TestCase):
    def test_all_success_is_complete(self):
        c = tab.completeness(metrics(), 0.9)
        self.assertEqual(c["verdict"], "完整")
        self.assertEqual(c["coverage_pct"], 100.0)
        self.assertEqual(c["failed"], [])

    def test_partial_failure_keeps_other_conclusions_valid(self):
        c = tab.completeness(metrics(targets=10, failed=1), 0.9)
        self.assertEqual(c["verdict"], "部分缺失")
        self.assertEqual(len(c["failed"]), 1)
        self.assertEqual(c["coverage_pct"], 90.0)
        self.assertIn("不形成买点结论", c["reason"])

    def test_coverage_below_threshold_is_unusable(self):
        self.assertEqual(tab.completeness(metrics(targets=10, failed=2), 0.9)["verdict"], "不可用")
        self.assertIn("阈值", tab.completeness(metrics(targets=10, failed=2), 0.9)["reason"])

    def test_market_data_missing_is_flagged_partial_not_scrapped(self):
        # 市场子项偶发缺失（实测涨跌家数）不应让整包作废：结论保留但标注不可执行
        c = tab.completeness(metrics(data_ok=False), 0.9)
        self.assertEqual(c["verdict"], "部分缺失")
        self.assertFalse(c["market_data_ok"])
        self.assertIn("不可执行", c["reason"])

    def test_missing_or_broken_metrics_is_unusable(self):
        for bad in (None, {}, "not-a-dict", []):
            self.assertEqual(tab.completeness(bad, 0.9)["verdict"], "不可用")

    def test_no_targets_is_unusable(self):
        self.assertEqual(tab.completeness(metrics(targets=0), 0.9)["verdict"], "不可用")

    def test_threshold_is_configurable(self):
        self.assertEqual(tab.completeness(metrics(targets=10, failed=5), 0.4)["verdict"], "部分缺失")
        self.assertEqual(tab.completeness(metrics(targets=10, failed=5), 0.6)["verdict"], "不可用")

    def test_explicit_zero_success_is_not_replaced_by_target_count(self):
        m = metrics()
        m["analyzed"] = 0
        self.assertEqual(tab.completeness(m)["verdict"], "不可用")

    def test_market_missing_details_survive_packaging(self):
        m = metrics(data_ok=False)
        m["market"]["missing"] = ["赚钱效应：涨跌家数数据缺失"]
        check = tab.completeness(m)
        self.assertEqual(check["market_missing"], m["market"]["missing"])
        self.assertIn("涨跌家数", check["reason"])


class ResearchCompletenessTests(unittest.TestCase):
    def analyze(self, count=59, bad_volume=False):
        now = datetime.now()
        bars = [dict(day=(now - timedelta(days=count-i)).strftime('%Y-%m-%d'),
                     open=10.1, close=10.1, high=10.4, low=10., volume=30000000)
                for i in range(count)]
        if bad_volume:
            bars[0]["volume"] = 0
        quote = dict(cur=10.1, prev=10.1, open=10.1, high=10.4, low=10., vol=20000000,
                     date=now.strftime('%Y-%m-%d'), time=now.strftime('%H:%M:%S'))
        with patch.dict(os.environ, ANALYSIS_ONLY="1"), \
             patch.object(st, "get_rt", return_value=quote), \
             patch.object(st.daily_history, "get", return_value=bars), \
             patch.object(st, "quote_is_fresh", return_value=True), \
             patch.object(st, "MARKET", dict(state="B", score=70, data_ok=True)), \
             patch.object(st, "ANALYSIS_STATS", dict(targets=0, failed=[])), \
             patch.object(st, "ENTRY_REVIEWS", []), patch.object(st, "FINAL_LIST", []), \
             patch.object(st.decision_manager, "load_states", return_value={}):
            report = st.analyze_item("600001", "测试", 0, is_etf=False)
            return report, st.analysis_metrics()

    def test_59_completed_plus_today_is_failure(self):
        report, m = self.analyze()
        self.assertIn("启动分析数据不足", report)
        self.assertIn("60根", report)
        self.assertEqual(m["analyzed"], 0)
        self.assertEqual(len(m["failed"]), 1)
        self.assertEqual(tab.completeness(m)["verdict"], "不可用")

    def test_invalid_historical_volume_is_failure(self):
        report, m = self.analyze(count=70, bad_volume=True)
        self.assertIn("数据不足", report)
        self.assertEqual(m["analyzed"], 0)

    def test_valid_research_is_still_success(self):
        report, m = self.analyze(count=70)
        self.assertIn("启动形态研究", report)
        self.assertEqual(m["analyzed"], 1)
        self.assertEqual(tab.completeness(m)["verdict"], "完整")

    def test_market_failure_reports_component_without_opening_gate(self):
        with patch.object(st, "MARKET", {}), \
             patch.object(st, "_trend_score", return_value=(30, ["正常"])), \
             patch.object(st, "_breadth_score", return_value=(8, ["涨跌家数数据缺失(中性8分)"])), \
             patch.object(st, "_volume_score", return_value=(20, "正常")), \
             patch.object(st, "_external_score", return_value=(20, ["正常"])):
            report = st.market_score({"上证指数": {"price": 10}})
            self.assertEqual(st.MARKET["state"], "UNKNOWN")
            self.assertFalse(st.MARKET["data_ok"])
            self.assertIn("赚钱效应", "\n".join(report))
            self.assertIn("涨跌家数", st.analysis_metrics()["market"]["missing"][0])


class AnalysisSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, attr, value in ((runtime, "DATA_DIR", self.root),
                                    (tab, "SCRIPT_DIR", str(self.root)),
                                    (tab, "pool_meta", lambda: {})):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.ts = "20260922111000"

    def capture_upload(self, command, **kwargs):
        source = Path(command[command.index("--source-dir") + 1]) / "technical_analysis_latest.json"
        self.sent = source.read_bytes()
        self.command = command
        return subprocess.CompletedProcess(command, 0)

    def test_latest_changes_do_not_change_retry_source(self):
        snapshot = Path(tab.write_bundle("original", "", tab.completeness(metrics()), self.ts))
        original = snapshot.read_bytes()
        tab.write_bundle("newer", "", tab.completeness(metrics()), "20260922133000")
        with patch.object(tab.subprocess, "run", side_effect=self.capture_upload):
            self.assertEqual(tab.upload("测试", self.ts), 0)
        self.assertEqual(self.sent, original)
        self.assertEqual(self.command[self.command.index("--ts") + 1], self.ts)

    def test_retry_legacy_archive_uses_exact_original_bytes(self):
        archive = self.root / "data_packages" / f"technical_analysis_latest_{self.ts}.json"
        archive.parent.mkdir()
        original = b'{"schema":"technical-analysis-bundle/v1","report":"old"}'
        archive.write_bytes(original)
        runtime.atomic_json(self.root / "technical_analysis_latest.json", {"report": "new"})
        with patch.object(tab.subprocess, "run", side_effect=self.capture_upload):
            tab.upload("测试", self.ts)
        self.assertEqual(self.sent, original)

    def test_unknown_retry_refuses_latest_fallback(self):
        runtime.atomic_json(self.root / "technical_analysis_latest.json", {"report": "new"})
        with patch.object(tab.subprocess, "run") as child:
            with self.assertRaises(ValueError):
                tab.upload("测试", self.ts)
            child.assert_not_called()

    def test_same_timestamp_cannot_overwrite_snapshot(self):
        snapshot = Path(tab.write_bundle("old", "", tab.completeness(metrics()), self.ts))
        original = snapshot.read_bytes()
        with self.assertRaises(FileExistsError):
            tab.write_bundle("new", "", tab.completeness(metrics()), self.ts)
        self.assertEqual(snapshot.read_bytes(), original)

    def test_upload_failure_keeps_original_for_retry(self):
        snapshot = Path(tab.write_bundle("old", "", tab.completeness(metrics()), self.ts))
        original = snapshot.read_bytes()
        with patch.object(tab.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            self.assertEqual(tab.upload("测试", self.ts), 1)
        self.assertEqual(snapshot.read_bytes(), original)

    def test_snapshot_is_subject_to_archive_completeness_gate(self):
        path = Path(tab.write_bundle("bad", "", tab.completeness({}), self.ts))
        with self.assertRaises(ValueError):
            upload.archive("测试", dry_run=True, files=["technical_analysis_latest.json"],
                           source_dir=path.parent, ts=self.ts)

    def test_main_upload_uses_snapshot_and_records_retry_id(self):
        with patch.object(sys, "argv", ["tech_analysis_bundle.py", "--task", "测试"]), \
             patch.object(tab, "pinned_current", return_value=nullcontext()), \
             patch.object(tab, "run_analysis", return_value=("report", "")), \
             patch.object(tab, "read_metrics", return_value=metrics()), \
             patch.object(tab.subprocess, "run", side_effect=self.capture_upload):
            with self.assertRaises(SystemExit) as ended:
                tab.main()
        self.assertEqual(ended.exception.code, 0)
        payload = json.loads(self.sent)
        ts = payload["archive_ts"]
        self.assertEqual(self.command[self.command.index("--ts") + 1], ts)
        self.assertTrue((self.root / "analysis_runs" / ts / "technical_analysis_latest.json").is_file())
        records = [json.loads(p.read_text(encoding="utf-8")) for p in (self.root / "analysis_runs").glob("*.json")]
        self.assertTrue(any(r.get("archived") and r.get("archive_ts") == ts for r in records))

    def test_retry_cli_does_not_rerun_analysis(self):
        tab.write_bundle("old", "", tab.completeness(metrics()), self.ts)
        with patch.object(sys, "argv", ["tech_analysis_bundle.py", "--retry-upload", self.ts]), \
             patch.object(tab, "run_analysis") as analysis, \
             patch.object(tab.subprocess, "run", side_effect=self.capture_upload):
            with self.assertRaises(SystemExit) as ended:
                tab.main()
        self.assertEqual(ended.exception.code, 0)
        analysis.assert_not_called()
        self.assertEqual(json.loads(self.sent)["report"], "old")


class ArchiveGateTests(unittest.TestCase):
    def test_analysis_package_without_verdict_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            src = Path(temp) / "technical_analysis_latest.json"
            for payload in ('{"schema": "technical-analysis-bundle/v1"}',
                            '{"completeness": {"verdict": "不可用"}}'):
                src.write_text(payload, encoding="utf-8")
                with self.assertRaises(ValueError):
                    upload.archive("测试", files=["technical_analysis_latest.json"], source_dir=temp)

    def test_leftover_archive_staging_is_healed(self):
        extra = {"data_packages/technical_analysis_latest_20260922111000.json"}
        self.assertEqual(upload.classify_staged_extras(extra, set()), sorted(extra))

    def test_unrelated_staged_files_are_refused(self):
        for bad in ({"stock_pool.py"},
                    {"data_packages/technical_analysis_latest_20260922111000.json.bak"},
                    {"data_packages/technical_analysis_latest_notats.json"},
                    {"other/technical_analysis_latest_20260922111000.json"},
                    {"data_packages/stock_pool_20260922123000.json", "notes.md"}):
            with self.assertRaises(RuntimeError):
                upload.classify_staged_extras(bad, set())

    def test_already_committed_archive_is_not_silently_dropped(self):
        tracked = {"data_packages/stock_pool_20260922123000.json"}
        with self.assertRaises(RuntimeError):
            upload.classify_staged_extras(tracked, tracked)

    def test_forced_timestamp_requires_fourteen_digits(self):
        self.assertTrue(upload.TS_PATTERN.match("20260922111000"))
        self.assertIsNone(upload.TS_PATTERN.match("2026-09-22"))


class StagingSelfHealTests(unittest.TestCase):
    def test_killed_run_leftover_is_unstaged_and_push_continues(self):
        """上一轮被杀后遗留的暂存归档不得永久卡死发布：自动 reset 后继续提交推送。"""
        import subprocess
        from unittest.mock import patch
        leftover = "data_packages/technical_analysis_latest_20260922111000.json"
        staged = [leftover]

        def run(command, **kwargs):
            if "diff" in command and "--cached" in command:
                return subprocess.CompletedProcess(command, 0, "\n".join(staged) + "\n", "")
            if command[:3] == ["git", "ls-tree", "-r"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            if "--show-current" in command:
                return subprocess.CompletedProcess(command, 0, "main\n", "")
            if "rev-parse" in command:
                return subprocess.CompletedProcess(command, 0, "abc123\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        commands = []
        with patch.object(subprocess, "run", side_effect=lambda c, **k: (commands.append(c), run(c, **k))[1]):
            short = upload.git_push("测试", "batch", [("pool.json", "source", 10)])
        self.assertEqual(short, "abc123")
        self.assertIn(["git", "reset", "-q", "--", leftover], commands)
        self.assertIn(["git", "push", "origin", "main"], commands)


class PublishLockTests(unittest.TestCase):
    def test_overlap_fails_fast_and_lock_releases(self):
        with runtime.publish_lock(0):
            with self.assertRaises(OSError):
                with runtime.publish_lock(0):
                    self.fail("overlapping publish acquired the shared lock")
        with runtime.publish_lock(0):
            pass

    def test_pool_pipeline_shares_the_same_lock(self):
        import pool_pipeline
        with runtime.publish_lock(0):
            with self.assertRaises(OSError):
                with pool_pipeline.pipeline_lock():
                    self.fail("analysis chain and pool chain must share one lock")
        with pool_pipeline.pipeline_lock():
            pass

    def test_child_of_lock_holder_is_bypassed(self):
        os.environ[runtime.PUBLISH_LOCK_HELD_ENV] = "1"
        try:
            with runtime.publish_lock(0):
                with runtime.publish_lock(0):
                    pass  # 子进程放行，不得自锁
        finally:
            os.environ.pop(runtime.PUBLISH_LOCK_HELD_ENV, None)

    def test_pipeline_passes_held_marker_to_children(self):
        import pool_pipeline
        p = pool_pipeline.Pipeline("测试")
        self.assertEqual(p.env.get(runtime.PUBLISH_LOCK_HELD_ENV), "1")


if __name__ == "__main__":
    unittest.main()
