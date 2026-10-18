"""市场数据源兜底 + 归档上传自愈 的离线契约（2026-09-23）。

背景（实测）：
  ① push2delay.eastmoney.com 域名间歇黑洞（TLS 握手完成后服务端空响应），
     涨跌家数(ulist)与隔夜美股(f170)双双缺失 → 市场评分 data_ok=False →
     池链整轮被判"降级研究"（不出正式池），分析包标"仅供研究不可执行"。
  ② 本机只推 data_packages/ 归档、开发机推裁决结果(watchlist.json) → 分叉后
     push 恒被 "fetch first" 拒绝，四个任务的归档上传连续失败。

本套件只覆盖离线逻辑：网络/子进程调用全部打桩，不联网、不写生产数据。
断言只看涨跌家数这一段的得分与来源标签（涨停/跌停段走外网，直接跑与离线跑结果不同）。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime
import short_term as st
import data_package_upload as upload


class BreadthFallbackTests(unittest.TestCase):
    def test_injected_counts_used_without_network(self):
        """池链传入的统计直接采用（零额外请求）。"""
        with patch.object(st, "_em_breadth_counts", side_effect=AssertionError("不应调用")) as em, \
             patch.object(st, "_sina_breadth_counts", side_effect=AssertionError("不应调用")):
            _score, detail = st._breadth_score((1891, 3563, 113, "本轮全市场扫描"))
        self.assertFalse(em.called)
        self.assertIn("涨1891/跌3563/平113", detail[0])
        self.assertIn("得0分", detail[0])          # 上涨33.9% → 0分
        self.assertIn("本轮全市场扫描", detail[0])

    def test_eastmoney_preferred_when_available(self):
        with patch.object(st, "_em_breadth_counts", return_value=(3800, 1900, 67, "东财")), \
             patch.object(st, "_sina_breadth_counts", side_effect=AssertionError("不该兜底")):
            _score, detail = st._breadth_score()
        self.assertIn("·东财", detail[0])
        self.assertIn("得15分", detail[0])

    def test_sina_fallback_when_eastmoney_blackholed(self):
        with patch.object(st, "_em_breadth_counts", side_effect=ValueError("东财涨跌家数不可用")), \
             patch.object(st, "_sina_breadth_counts", return_value=(1891, 3563, 113, "新浪全市场扫描")):
            _score, detail = st._breadth_score()
        self.assertIn("新浪全市场扫描", detail[0])

    def test_both_sources_dead_keeps_missing_neutral(self):
        """两源都不可用 → 仍记"缺失"(中性8分)，不猜数据。"""
        with patch.object(st, "_em_breadth_counts", side_effect=ValueError("x")), \
             patch.object(st, "_sina_breadth_counts", side_effect=ValueError("y")):
            _score, detail = st._breadth_score()
        self.assertEqual(detail[0], "涨跌家数数据缺失(中性8分)")

    def test_breadth_from_rows_counts_up_down_flat(self):
        rows = [{"changepercent": 1.2}, {"changepercent": -0.5}, {"changepercent": 0},
                {"changepercent": None}, {"changepercent": "3.1"}, {"changepercent": "-2"}]
        self.assertEqual(st.breadth_from_rows(rows), (2, 2, 1, "本轮全市场扫描"))
        self.assertIsNone(st.breadth_from_rows([]))


class SinaBreadthCacheTests(unittest.TestCase):
    def test_incomplete_market_list_is_rejected(self):
        with patch("stock_scanner.fetch_all_stocks", return_value=[{"changepercent": 1}]), \
             patch("stock_scanner.LAST_FETCH_COMPLETE", False):
            with self.assertRaises(ValueError):
                st._sina_breadth_counts(force=True)

    def test_counts_cached_then_reused_within_ttl(self):
        today = st.datetime.now().strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as tmp, patch.object(runtime, "DATA_DIR", Path(tmp)), \
             patch("stock_scanner.fetch_all_stocks",
                   return_value=[{"changepercent": 2}, {"changepercent": -1},
                                 {"changepercent": 0}]) as fetch, \
             patch("stock_scanner.LAST_FETCH_COMPLETE", True):
            first = st._sina_breadth_counts(force=True)
            second = st._sina_breadth_counts()
            cache = json.loads(Path(runtime.data_path("breadth_cache.json")).read_text(encoding="utf-8"))
        self.assertEqual(first[0:3], (1, 1, 1))
        self.assertEqual(second[0:3], (1, 1, 1))
        self.assertEqual(second[3], "新浪全市场扫描")
        self.assertEqual(fetch.call_count, 1)  # 第二次命中缓存
        self.assertEqual(cache["day"], today)
        self.assertEqual(cache["up"], 1)


class ExternalFallbackTests(unittest.TestCase):
    def test_sina_us_change_parses_change_percent(self):
        payload = 'var hq_str_gb_ndx="纳斯达克100,30732.3956,0.82,2026-09-23 09:48:45,250.0431";'.encode("utf-8")
        with patch.object(st, "sina_get", return_value=payload):
            self.assertAlmostEqual(st._sina_us_change("ndx"), 0.82)

    def test_external_uses_sina_when_eastmoney_dead(self):
        with patch.object(st, "em_get", side_effect=OSError("empty reply")), \
             patch.object(st, "_sina_us_change", side_effect=[0.82, 1.31]):
            score, detail = st._external_score()
        self.assertEqual(score, 8 + 9)
        self.assertTrue(all("·新浪" in item for item in detail))

    def test_external_missing_when_both_dead(self):
        with patch.object(st, "em_get", side_effect=OSError("empty reply")), \
             patch.object(st, "_sina_us_change", side_effect=ValueError("缺失")):
            score, detail = st._external_score()
        self.assertEqual(score, 6 + 5)
        self.assertTrue(all("缺失" in item for item in detail))


class MarketScoreGateTests(unittest.TestCase):
    def test_all_components_ok_gives_named_market_state(self):
        with patch.object(st, "get_indices", return_value={"上证指数": {"price": 10}}), \
             patch.object(st, "_trend_score", return_value=(30, ["正常"])), \
             patch.object(st, "_volume_score", return_value=(20, "正常")), \
             patch.object(st, "_external_score", return_value=(20, ["正常"])), \
             patch.object(st, "_breadth_score",
                          return_value=(30, ["涨3600/跌1900/平67(上涨65%得15分·东财)",
                                             "涨停51/跌停3(净+48得12分)"])):
            st.market_score(breadth=(3600, 1900, 67, "本轮全市场扫描"))
        self.assertTrue(st.MARKET["data_ok"])
        self.assertIn(st.MARKET["state"], ("A", "B", "C", "D"))
        self.assertEqual(st.MARKET.get("missing", []), [])

    def test_missing_breadth_still_unknown(self):
        with patch.object(st, "get_indices", return_value={"上证指数": {"price": 10}}), \
             patch.object(st, "_trend_score", return_value=(30, ["正常"])), \
             patch.object(st, "_volume_score", return_value=(20, "正常")), \
             patch.object(st, "_external_score", return_value=(20, ["正常"])), \
             patch.object(st, "_em_breadth_counts", side_effect=ValueError("x")), \
             patch.object(st, "_sina_breadth_counts", side_effect=ValueError("y")):
            lines = st.market_score()
        self.assertFalse(st.MARKET["data_ok"])
        self.assertEqual(st.MARKET["state"], "UNKNOWN")
        self.assertTrue(any("涨跌家数" in ln for ln in lines))

    def test_breadth_ok_but_limit_pool_missing_is_unknown(self):
        """涨跌家数有值但涨停/跌停缺失 → 仍 UNKNOWN（不拿半份数据冒充完整评分）。"""
        with patch.object(st, "get_indices", return_value={"上证指数": {"price": 10}}), \
             patch.object(st, "_trend_score", return_value=(30, ["正常"])), \
             patch.object(st, "_volume_score", return_value=(20, "正常")), \
             patch.object(st, "_external_score", return_value=(20, ["正常"])), \
             patch.object(st, "_breadth_score",
                          return_value=(23, ["涨3600/跌1900/平67(上涨65%得15分·东财)",
                                             "涨停/跌停数据缺失(中性8分)"])):
            st.market_score(breadth=(3600, 1900, 67, "本轮全市场扫描"))
        self.assertFalse(st.MARKET["data_ok"])
        self.assertEqual(st.MARKET["state"], "UNKNOWN")


class UploadReconcileTests(unittest.TestCase):
    class _Proc:
        def __init__(self, rc=0, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = rc, stdout, stderr

    def test_automerge_disabled_raises(self):
        with patch.dict(os.environ, {"PACKAGE_AUTOMERGE": "0"}):
            with self.assertRaises(RuntimeError):
                upload._reconcile_remote_before_push(lambda cmd, check=True: self._Proc())

    def test_remote_code_changes_refused(self):
        def fake_runner(cmd, check=True):
            return self._Proc(stdout="watchlist.json\nstock_pool.py\n")

        with patch.dict(os.environ, {"PACKAGE_AUTOMERGE": "1"}), \
             patch.object(upload.subprocess, "run", return_value=self._Proc()):
            with self.assertRaises(RuntimeError) as ctx:
                upload._reconcile_remote_before_push(fake_runner)
        self.assertIn("stock_pool.py", str(ctx.exception))

    def test_remote_data_only_merges(self):
        def fake_runner(cmd, check=True):
            if cmd[:3] == ["git", "diff", "--name-only"]:
                return self._Proc(stdout="watchlist.json\ndata_packages/a_20260923.json\n")
            return self._Proc()

        with patch.dict(os.environ, {"PACKAGE_AUTOMERGE": "1"}), \
             patch.object(upload.subprocess, "run", return_value=self._Proc()) as run:
            result = upload._reconcile_remote_before_push(fake_runner)
        self.assertEqual(result, "merged")
        commands = [" ".join(call.args[0]) for call in run.call_args_list]
        self.assertTrue(any(cmd.startswith("git fetch origin main") for cmd in commands))
        self.assertTrue(any(cmd.startswith("git merge origin/main") for cmd in commands))

    def test_remote_in_sync_needs_no_merge(self):
        def fake_runner(cmd, check=True):
            return self._Proc(stdout="")

        with patch.dict(os.environ, {"PACKAGE_AUTOMERGE": "1"}), \
             patch.object(upload.subprocess, "run", return_value=self._Proc()) as run:
            result = upload._reconcile_remote_before_push(fake_runner)
        self.assertEqual(result, "already-synced")
        self.assertEqual(run.call_count, 1)  # 只有 fetch


if __name__ == "__main__":
    unittest.main(verbosity=2)
