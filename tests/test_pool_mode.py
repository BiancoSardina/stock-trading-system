"""数据分级（正式 / 降级研究 / 停止）+ 降级研究候选契约的离线测试。

规则来源：2026-09-22 用户确认——"软化研究流程，不软化数据真实性与交易风控"。
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pool_mode as pm
import pool_batch
import stock_pool as pool
import data_package_upload as upload

TODAY = datetime.now().strftime("%Y-%m-%d")
BATCH_ID = "20260922180000_abcdef12"


def classify(**kw):
    base = dict(market_status="B", market_data_ok=True, market_missing=(), fetch_complete=True,
                industry_count=49, industry_failed=(), benchmark_ok=True,
                candidate_failures=0, candidate_count=1000, scan_complete=True)
    base.update(kw)
    return pm.classify(**base)


def research_payload(**kw):
    record = classify(market_status="UNKNOWN", market_data_ok=False, market_missing=["赚钱效应：涨跌家数数据缺失"])
    payload = pm.build_research_payload(
        record, date=TODAY, core_pool=[{"code": "600000", "name": "测试"}], watch_pool=[],
        stats={"buy_states": {"等待": 1}}, scan_metrics={"complete": True},
        formal_pool={"batch_id": "20260918180500_11223344", "date": TODAY,
                     "generated_at": TODAY + " 18:05:32", "market_status": "A"},
        batch_id=BATCH_ID)
    payload.update(kw)
    return payload


def entry(code, industry, score=65, industry_score=60, triggered=False):
    return dict(code=code, name="测试" + code, stock_score=score, total_score=score,
                industry=industry, industry_score=industry_score,
                opportunity=dict(candidate=True, triggered=triggered, setup="整理启动型",
                                 reasons=[] if triggered else ["非连续交易时段，仅生成预案"]),
                position=dict(deduct=0, rise20=1.0, distance_ma20=1.0, rsi14=50.0, reasons=[]))


class ClassifyTests(unittest.TestCase):
    def test_complete_data_is_formal(self):
        r = classify()
        self.assertEqual(r["mode"], pm.MODE_FORMAL)
        self.assertEqual(r["reasons"], [])

    def test_market_missing_degrades_never_stops(self):
        r = classify(market_status="UNKNOWN", market_data_ok=False,
                     market_missing=["赚钱效应：涨跌家数数据缺失"])
        self.assertEqual(r["mode"], pm.MODE_DEGRADED)
        self.assertEqual(r["stop_reasons"], [])
        self.assertTrue(any("市场评分不可用" in x for x in r["degraded_reasons"]))
        self.assertTrue(any("涨跌家数" in x for x in r["degraded_reasons"]))

    def test_benchmark_missing_degrades(self):
        self.assertEqual(classify(benchmark_ok=False)["mode"], pm.MODE_DEGRADED)

    def test_industry_shortfall_degrades_and_severe_stops(self):
        self.assertEqual(classify(industry_count=39, industry_failed=["农林牧渔"])["mode"], pm.MODE_DEGRADED)
        self.assertEqual(classify(industry_count=19)["mode"], pm.MODE_STOP)
        self.assertEqual(classify(industry_count=0)["mode"], pm.MODE_STOP)

    def test_fetch_incomplete_stops(self):
        self.assertEqual(classify(fetch_complete=False)["mode"], pm.MODE_STOP)

    def test_scan_incomplete_stops(self):
        self.assertEqual(classify(scan_complete=False)["mode"], pm.MODE_STOP)

    def test_failure_rate_threshold_keeps_existing_allowance(self):
        self.assertEqual(classify(candidate_failures=15, candidate_count=1000)["mode"], pm.MODE_FORMAL)
        self.assertEqual(classify(candidate_failures=60, candidate_count=3025)["mode"], pm.MODE_FORMAL)
        self.assertEqual(classify(candidate_failures=61, candidate_count=3025)["mode"], pm.MODE_STOP)


class ResearchArtifactTests(unittest.TestCase):
    def test_payload_labels_and_never_claims_tradeable(self):
        p = research_payload()
        self.assertEqual(p["mode"], pm.MODE_DEGRADED)
        self.assertFalse(p["tradeable"])
        self.assertTrue(p["not_a_buy_signal"])
        self.assertEqual(p["market"]["state"], "UNKNOWN")
        self.assertIsNone(p["market"]["score"])           # 不得用中性分伪装完整评分
        self.assertTrue(p["degraded_reasons"])
        self.assertTrue(p["formal_pool"]["batch_id"])     # 上一批正式池身份留档
        pm.validate_research_payload(p)

    def test_validator_rejects_disguised_or_incomplete_labels(self):
        for bad in (dict(market={"state": "C", "score": 51}),   # 不得改写成 C/D
                    dict(tradeable=True),
                    dict(not_a_buy_signal=False),
                    dict(degraded_reasons=[]),
                    dict(mode=pm.MODE_FORMAL),
                    dict(schema="something-else"),
                    dict(date="2020-01-01")):                    # 过期数据不可用
            with self.assertRaises(ValueError):
                pm.validate_research_payload(research_payload(**bad))

    def test_formal_publish_rejects_degraded_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            d = Path(temp)
            (d / "stock_pool.json").write_text(json.dumps(dict(
                data_ok=True, date=TODAY, generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                market_status="B", market_score=70, mode=pm.MODE_DEGRADED)), encoding="utf-8")
            (d / "decision_bundle_latest.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                pool_batch.validate_pair(d)
            self.assertIn("Degraded research candidates", str(ctx.exception))

    def test_batch_helper_validates_research_file(self):
        with tempfile.TemporaryDirectory() as temp:
            d = Path(temp)
            (d / pm.RESEARCH_FILE).write_text(json.dumps(research_payload(), ensure_ascii=False),
                                              encoding="utf-8")
            self.assertEqual(pool_batch.validate_research(d)["mode"], pm.MODE_DEGRADED)


class ProvenanceTests(unittest.TestCase):
    def test_fresh_formal_pool_wins(self):
        prov = pm.pool_provenance(formal_pool={"date": TODAY, "mode": pm.MODE_FORMAL})
        self.assertEqual(prov["provenance"], pm.PROVENANCE_FORMAL)
        self.assertTrue(prov["tradeable"])
        self.assertFalse(prov["degraded"])

    def test_stale_formal_pool_falls_back_to_degraded_research(self):
        with tempfile.TemporaryDirectory() as temp:
            d = Path(temp)
            (d / pm.RESEARCH_LATEST_FILE).write_text(json.dumps(research_payload(), ensure_ascii=False),
                                                     encoding="utf-8")
            with patch.object(pm, "DATA_DIR", d):
                prov = pm.pool_provenance(formal_pool={"date": "2026-01-01", "mode": pm.MODE_FORMAL})
        self.assertEqual(prov["provenance"], pm.PROVENANCE_DEGRADED)
        self.assertTrue(prov["degraded"])
        self.assertFalse(prov["tradeable"])

    def test_degraded_file_cannot_disguise_as_formal(self):
        with tempfile.TemporaryDirectory() as temp:
            d = Path(temp)
            (d / pm.RESEARCH_LATEST_FILE).write_text(json.dumps(research_payload(), ensure_ascii=False),
                                                     encoding="utf-8")
            with patch.object(pm, "DATA_DIR", d):
                self.assertIsNone(pm.read_research(path=str(d / "missing.json")))
                self.assertEqual(pm.read_research()["mode"], pm.MODE_DEGRADED)

    def test_fallback_when_nothing_usable(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(pm, "DATA_DIR", Path(temp)):
                prov = pm.pool_provenance(formal_pool={})
        self.assertEqual(prov["provenance"], pm.PROVENANCE_FALLBACK)
        self.assertFalse(prov["tradeable"])


class QualityOnlyPoolTests(unittest.TestCase):
    def test_research_path_targets_batch_and_latest(self):
        # 流水线内写批次目录（供归档/重试），独立运行写运行数据目录（供技术分析读取）
        with patch.dict(os.environ, POOL_BATCH_DIR="/tmp/xx_batch"):
            self.assertEqual(pm.research_write_path(), "/tmp/xx_batch/research_candidates.json")
        self.assertEqual(pm.research_write_path(), str(pm.DATA_DIR / pm.RESEARCH_LATEST_FILE))

    def test_degraded_run_still_classifies_by_quality(self):
        rows = [entry("600000", "银行", triggered=False), entry("600001", "钢铁", industry_score=50)]
        core, watch, stats = pool.generate_pool(rows, "UNKNOWN", None, TODAY, quality_only=True)
        self.assertEqual([e["code"] for e in core], ["600000"])
        self.assertEqual([e["code"] for e in watch], ["600001"])
        self.assertIn("buy_states", stats)

    def test_formal_run_still_refuses_unknown_market(self):
        rows = [entry("600000", "银行")]
        core, watch, stats = pool.generate_pool(rows, "UNKNOWN", None, TODAY)
        self.assertFalse(core + watch)
        self.assertIn("error", stats)


class ArchiveResearchTests(unittest.TestCase):
    def test_research_archive_validated_and_stable_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            d = Path(temp)
            (d / pm.RESEARCH_FILE).write_text(json.dumps(research_payload(), ensure_ascii=False),
                                              encoding="utf-8")
            ts, archived, note = upload.archive("收盘股票池", dry_run=True,
                                                files=[pm.RESEARCH_FILE], source_dir=temp)
            self.assertEqual(ts, BATCH_ID)
            self.assertEqual(archived[0][0], f"research_candidates_{BATCH_ID}.json")
            self.assertIn("降级研究候选", note)
            self.assertIn("未更新正式池", note)
            self.assertFalse((Path(upload.PACKAGE_DIR) / archived[0][0]).exists())

    def test_disguised_market_grade_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            d = Path(temp)
            (d / pm.RESEARCH_FILE).write_text(json.dumps(research_payload(market={"state": "C", "score": 51}),
                                                         ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                upload.archive("收盘股票池", dry_run=True, files=[pm.RESEARCH_FILE], source_dir=temp)


if __name__ == "__main__":
    unittest.main()
