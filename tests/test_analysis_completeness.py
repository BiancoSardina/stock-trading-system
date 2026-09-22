"""分析完整性三态 + 归档门禁 + 暂存残留自愈 + 跨链发布锁 的离线契约。

背景（2026-09-22 用户指出）：
  ① 技术分析只检查"退出码0 + 报告非空"就打包，部分标的失败也会被当成有效结论；
  ② 池链与技术分析链共用同一个 git 工作区，却没有共同的发布锁。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime
import tech_analysis_bundle as tab
import data_package_upload as upload


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
