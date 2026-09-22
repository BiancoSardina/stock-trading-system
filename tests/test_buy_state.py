"""买点状态（时点量）与质量分级（core/watch）解耦的离线契约（无网络、无写盘）。

背景（2026-09-22 用户指出）：原来 CORE 要求"此刻买点已触发"，
导致午休(11:30-13:00)与盘后(15:00 后)扫描必然出不了 CORE。
现在 CORE/WATCH 只看形态质量与行业；买点状态单列为 条件满足/等待/失效。
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import startup_policy as policy
import stock_pool as pool

IN_SESSION = datetime(2026, 9, 22, 10, 30)
LUNCH = datetime(2026, 9, 22, 12, 30)
AFTER_CLOSE = datetime(2026, 9, 22, 18, 0)


def entry(code, industry, score=65, industry_score=60, triggered=False, candidate=True):
    return dict(code=code, name="测试" + code, stock_score=score, total_score=score,
                industry=industry, industry_score=industry_score,
                opportunity=dict(candidate=candidate, triggered=triggered, setup="整理启动型",
                                 reasons=[] if triggered else ["现价不在条件买入区间，不随现价上移区间"]),
                position=dict(deduct=0, rise20=1.0, distance_ma20=1.0, rsi14=50.0, reasons=[]))


class BuyStateTests(unittest.TestCase):
    def test_three_states_track_condition_only(self):
        triggered = dict(candidate=True, triggered=True,
                         reasons=["形态、位置与报价转强条件满足；仅研究建议，不是订单"])
        waiting = dict(candidate=True, triggered=False,
                       reasons=["现价不在条件买入区间，不随现价上移区间"])
        stop_touched = dict(candidate=True, triggered=False,
                            reasons=["当日低点缺失或已触及失效位，取消本轮触发"])
        broken = dict(candidate=False, triggered=False, reasons=["跌破结构失效位"])
        self.assertEqual(policy.buy_state_record(triggered, now=IN_SESSION)["state"], "条件满足")
        self.assertEqual(policy.buy_state_record(waiting, now=IN_SESSION)["state"], "等待")
        self.assertEqual(policy.buy_state_record(stop_touched, now=IN_SESSION)["state"], "失效")
        self.assertEqual(policy.buy_state_record(broken, now=IN_SESSION)["state"], "失效")

    def test_off_session_is_waiting_with_recheck_note(self):
        waiting = dict(candidate=True, triggered=False, reasons=["非连续交易时段，仅生成预案"])
        lunch = policy.buy_state_record(waiting, now=LUNCH)
        close = policy.buy_state_record(waiting, now=AFTER_CLOSE)
        self.assertEqual(lunch["state"], "等待")
        self.assertTrue(lunch["off_session"])
        self.assertTrue(close["off_session"])
        self.assertIn("下一交易时段复核", lunch["note"])
        self.assertFalse(policy.buy_state_record(waiting, now=IN_SESSION)["off_session"])

    def test_record_is_timestamped_and_documented(self):
        rec = policy.buy_state_record(dict(candidate=True, triggered=False, reasons=[]), now=LUNCH)
        self.assertEqual(rec["as_of"], "2026-09-22 12:30:00")
        self.assertIn("时点", rec["time_independent_note"])
        self.assertIn(rec["state"], policy.BUY_STATES)

    def test_render_prints_buy_state(self):
        text = "\n".join(policy.render(dict(candidate=True, triggered=False, score=70,
                                            setup="整理启动型", factors={}, reasons=[],
                                            limitations=[], plan=None, status="候选待确认")))
        self.assertIn("买点状态", text)


class PoolClassificationTests(unittest.TestCase):
    def test_core_no_longer_requires_trigger(self):
        # 午休/盘后扫描：形态合格但买点未触发 → 依然是 core（研究价值不随时间变）
        rows = [entry("600000", "银行", triggered=False), entry("600001", "钢铁", triggered=True)]
        core, watch, stats = pool.generate_pool(rows, "B", None, "2026-09-22")
        self.assertEqual({e["code"] for e in core}, {"600000", "600001"})
        self.assertEqual(watch, [])
        self.assertEqual({e["level"] for e in core}, {"core"})
        self.assertEqual(stats["buy_states"], {"条件满足": 1, "等待": 1, "失效": 0})

    def test_classification_follows_quality_only(self):
        rows = [entry("600002", "银行", industry_score=60), entry("600003", "钢铁", industry_score=50)]
        core, watch, _ = pool.generate_pool(rows, "B", None, "2026-09-22")
        self.assertEqual([e["code"] for e in core], ["600002"])   # 行业≥55 → core
        self.assertEqual([e["code"] for e in watch], ["600003"])  # 行业中等 → watch
        for e in core + watch:
            self.assertIn(e["buy_state"]["state"], policy.BUY_STATES)

    def test_score_below_candidate_line_is_rejected(self):
        rows = [entry("600004", "银行", score=policy.MIN_SCORE - 1)]
        core, watch, _ = pool.generate_pool(rows, "B", None, "2026-09-22")
        self.assertFalse(core + watch)

    def test_lost_setup_is_evicted_regardless_of_buy_state(self):
        rows = [entry("600005", "银行", candidate=False)]
        core, watch, stats = pool.generate_pool(rows, "B", None, "2026-09-22")
        self.assertFalse(core + watch)
        self.assertTrue(stats["evicted"])

    def test_D_market_keeps_watch_but_no_core(self):
        rows = [entry("600006", "银行", triggered=True)]
        core, watch, _ = pool.generate_pool(rows, "D", None, "2026-09-22")
        self.assertEqual(core, [])
        self.assertEqual([e["code"] for e in watch], ["600006"])

    def test_industry_uniqueness_still_applies(self):
        rows = [entry("600007", "银行", score=70), entry("600008", "银行", score=66)]
        core, watch, _ = pool.generate_pool(rows, "B", None, "2026-09-22")
        self.assertEqual([e["code"] for e in core], ["600007"])
        self.assertEqual(watch, [])


if __name__ == "__main__":
    unittest.main()
