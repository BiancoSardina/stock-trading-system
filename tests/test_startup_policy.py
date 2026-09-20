"""Offline, synthetic contracts. These are not profitability/backtest evidence."""
import copy
import os
from pathlib import Path
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import startup_policy as policy
import stock_pool as pool
import stock_scanner as scanner
import short_term as st
import report_contract

NOW = datetime(2026, 9, 18, 10, 30)


def bars():
    # A flat base, with a historical overhead target. No breakout or big volume.
    result = []
    for i in range(70):
        p = 10.1
        result.append({"day": (NOW-timedelta(days=70-i)).strftime('%Y-%m-%d'),
                       "open": p, "close": p, "high": 10.4, "low": 10., "volume": 30000000})
    return result


def quote(price=10.1):
    return {"cur": price, "prev": 10.1, "open": 10.1, "low": 10., "high": 10.4,
            "date": "2026-09-18", "time": "10:30:00", "vol": 20000000}


def evaluate(data=None, q=None, market="B", **kwargs):
    return policy.evaluate(data or bars(), q or quote(), market, now=NOW, **kwargs)


class StartupTests(unittest.TestCase):
    def test_report_entrypoints_cannot_inherit_order_writing_mode(self):
        with patch.dict(os.environ, ANALYSIS_ONLY="0", QQ_SEND_DISABLE="0"):
            env = report_contract.analysis_environment()
            self.assertEqual(env['ANALYSIS_ONLY'], '1')
            self.assertEqual(env['QQ_SEND_DISABLE'], '1')
            self.assertEqual(os.environ['ANALYSIS_ONLY'], '0')

    def test_quiet_base_can_qualify_without_big_volume_or_strong_grade(self):
        r = evaluate()
        self.assertTrue(r["candidate"], r)
        self.assertEqual(r["setup"], "整理启动型")
        self.assertTrue(r["triggered"], r)
        self.assertNotIn("quantity", r)

    def test_rebound_can_qualify_below_ma60(self):
        data = bars()
        for b in data[:45]:
            b.update(open=12., close=12., high=12.2, low=11.8)
        r = evaluate(data)
        self.assertEqual(r["setup"], "超跌企稳型")
        self.assertTrue(r["candidate"], r)

    def test_falling_on_low_volume_is_not_stabilization(self):
        data = bars()
        for i, b in enumerate(data[-10:]):
            p = 10.8-i*.1
            b.update(open=p+.05, close=p, high=p+.1, low=p-.1, volume=30000000-i*1000000)
        self.assertFalse(evaluate(data, quote(9.9))["candidate"])

    def test_slow_low_volume_drift_is_also_rejected(self):
        data = bars()
        for i, b in enumerate(data[-5:]):
            p = 10.12-i*.005
            b.update(open=p+.01, close=p, low=10-i*.005, volume=20000000)
        self.assertFalse(evaluate(data)["candidate"])

    def test_D_and_unknown_never_trigger(self):
        for market in ("D", "UNKNOWN", None):
            r = evaluate(market=market)
            self.assertTrue(r["candidate"])
            self.assertFalse(r["triggered"])

    def test_rr_at_entire_interval_is_valid_before_costs(self):
        p = evaluate()["plan"]
        self.assertIsNotNone(p)
        self.assertLess(p["stop"], p["entry_low"])
        self.assertLess(p["entry_high"], p["target"])
        rr = (p["target"]-p["entry_high"])/(p["entry_high"]-p["stop"])
        self.assertGreaterEqual(rr+1e-10, policy.MIN_PRICE_RR)

    def test_current_quote_cannot_move_support_or_target(self):
        first, second = evaluate(), evaluate(q=quote(10.15))
        self.assertEqual(first["evidence"], second["evidence"])
        self.assertEqual(first["plan"]["entry_high"], second["plan"]["entry_high"])

    def test_future_and_partial_today_bars_are_ignored(self):
        data = bars()
        original = evaluate(data)
        for day in ("2026-09-18", "2026-09-19"):
            data.append({"day": day, "open": 99, "high": 100, "low": 1, "close": 99, "volume": 999999})
        self.assertEqual(original, evaluate(data))

    def test_missing_and_nonfinite_ohlcv_fail_closed(self):
        for key, value in (("volume", None), ("low", float('nan')), ("close", float('inf')), ("open", 99)):
            data = bars()
            data[-1][key] = value
            self.assertFalse(evaluate(data)["candidate"])
        data = bars(); data.append(dict(data[-1]))
        self.assertFalse(evaluate(data)["candidate"])

    def test_broken_support_and_extended_quotes_reject(self):
        for price in (9.8, 11.5):
            self.assertFalse(evaluate(q=quote(price))["candidate"])

    def test_high_score_does_not_override_intraday_weakness(self):
        r = evaluate(q=dict(quote(), open=10.3))
        self.assertTrue(r["candidate"])
        self.assertFalse(r["triggered"])
        self.assertIn("等待转强", "；".join(r["reasons"]))

    def test_stale_quote_and_non_trading_time_do_not_trigger(self):
        self.assertFalse(evaluate(q=dict(quote(), date="2026-09-17"))["candidate"])
        for now in (datetime(2026,9,18,12), datetime(2026,9,18,15,1), datetime(2026,9,19,10)):
            r = policy.evaluate(bars(), dict(quote(), date=now.strftime('%Y-%m-%d')), 'A', now=now)
            self.assertFalse(r["triggered"])

    def test_breakout_cannot_invent_target_and_C_cannot_chase(self):
        r = evaluate(q=quote(10.41))
        self.assertIsNone(r["plan"])
        self.assertFalse(r["triggered"])
        data = bars()
        for b in data[:30]:
            b.update(high=11.8)
        r = evaluate(data, quote(10.41), market="C")
        self.assertIn("市场C级仅低吸", "；".join(r["reasons"]))

    def test_relative_strength_uses_matching_dates(self):
        data = bars()
        index = {b["day"]: 100-i*.1 for i,b in enumerate(data)}
        r = evaluate(data, benchmark=index)
        self.assertGreater(r["factors"]["rs"], 0)
        index.pop(data[-6]["day"])
        self.assertEqual(evaluate(data, benchmark=index)["factors"]["rs"], 0)

    def test_scanner_profile_preserves_other_strategies_and_st_permissions(self):
        sample = dict(code="000690", name="测试", trade=4.95, amount=100000, changepercent=.1)
        self.assertFalse(scanner.basic_filter([sample])[0])
        self.assertTrue(scanner.basic_filter([sample], early_setups=True)[0])
        for change in (dict(name="ST测试"), dict(code="300001"), dict(trade=0), dict(trade=float('nan')), dict(changepercent=-10)):
            self.assertFalse(scanner.basic_filter([dict(sample, **change)], early_setups=True)[0])

    def test_pool_both_paths_survive_below_ma60_and_D_has_no_core(self):
        for setup in ("整理启动型", "超跌企稳型"):
            entry = dict(code="600577", name="测试", stock_score=65, total_score=65,
                         industry_score=60, industry="测试行业", trend=dict(above_ma60=False),
                         opportunity=dict(candidate=True, setup=setup, triggered=False))
            old = {"date": "2026-09-17", "watch_pool": [dict(entry, days_in_pool=1, first_seen="2026-09-17")]}
            core, watch, _ = pool.generate_pool([copy.deepcopy(entry)], "D", old, "2026-09-18")
            self.assertEqual(len(core), 0)
            self.assertEqual(watch[0]["days_in_pool"], 2)
            entry["opportunity"]["candidate"] = False
            core, watch, _ = pool.generate_pool([entry], "B", old, "2026-09-18")
            self.assertFalse(core+watch)

    def test_pool_score_uses_startup_factor_not_legacy_grade(self):
        data = bars()
        for b in data[:45]:
            b.update(open=12., close=12., high=12.2, low=11.8)
        with patch.object(pool, "datetime") as clock, patch.object(policy, "datetime") as policy_clock, \
             patch.object(st, "get_rt", return_value=quote()), patch.object(pool, "_get_kline", return_value=data), \
             patch.object(pool, "quote_is_fresh", return_value=True), patch.object(st, "MARKET", {"state": "B"}), \
             patch.object(st, "STARTUP_BENCHMARK", {}):
            clock.now.return_value = NOW
            policy_clock.now.return_value = NOW
            policy_clock.strptime = datetime.strptime
            r = pool.score_stock('600577', '测试', 60, None, None)
            self.assertNotIn('_exclude', r)
            self.assertEqual(r['score_basis'], policy.VERSION)
            self.assertEqual(r['stock_score'], sum(r['opportunity']['factors'].values()))
            self.assertFalse(r['trend']['above_ma60'])
            r.update(industry_score=60, industry='测试行业')
            core, watch, _ = pool.generate_pool([r], 'B', None, '2026-09-18')
            self.assertTrue(core+watch)
            self.assertFalse(pool.generate_pool([r], 'UNKNOWN', None, '2026-09-18')[0])

    def test_scoring_retains_historical_liquidity_gate(self):
        data = bars()
        for b in data: b['volume'] = 1000
        with patch.object(pool, "datetime") as clock, patch.object(policy, "datetime") as policy_clock, \
             patch.object(st, "get_rt", return_value=quote()), patch.object(pool, "_get_kline", return_value=data), \
             patch.object(pool, "quote_is_fresh", return_value=True):
            clock.now.return_value = NOW
            policy_clock.now.return_value = NOW
            policy_clock.strptime = datetime.strptime
            r = pool.score_stock('600577', '测试', 60, None, None)
            self.assertIn('流动性', r['_exclude'])

    def test_analysis_budget_does_not_change_research_or_call_execution(self):
        with patch.dict(os.environ, ANALYSIS_ONLY="1"), patch.object(st, "get_rt", return_value=quote()), \
             patch.object(st, "get_kline", return_value=bars()), patch.object(st, "quote_is_fresh", return_value=True), \
             patch.object(st, "datetime") as clock, patch.object(st.decision_manager, "load_states", return_value={}), \
             patch.object(st.decision_manager, "finalize") as finalize, \
             patch.object(st.startup_policy, "datetime") as policy_clock, \
             patch.object(st, "MARKET", {"state": "B"}), patch.object(st, "ENTRY_REVIEWS", []):
            clock.now.return_value = NOW
            policy_clock.now.return_value = NOW
            policy_clock.strptime = datetime.strptime
            first = st.analyze_item('600577', '测试', 0, total_amount=1, is_etf=False)
            second = st.analyze_item('600577', '测试', 0, total_amount=200000, is_etf=False)
            self.assertEqual(first, second)
            self.assertIn("启动机会", first)
            self.assertNotIn("预算不足", first)
            finalize.assert_not_called()


if __name__ == '__main__':
    unittest.main()
