"""Offline regression tests for audited trading and reporting defects."""
import copy
import csv
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime
import decision_manager as dm
import position_manager as pm
import short_term as st
import etf_t_engine as te
import paper_trader as pt
import paper_execution as pe
import stock_pool as sp
import stock_pool_ai as sai
import stock_picker_ai as spa
import stock_scanner as scanner
import watchlist
import review
import signal_store
import qq_send
from ai_decision import validate_verdict


def bars(prices, end=None):
    end = end or date.today()
    return [{"day": (end - timedelta(days=len(prices) - i)).isoformat(),
             "open": p, "close": p, "high": p * 1.01, "low": p * .99, "volume": 1000}
            for i, p in enumerate(prices)]


def quote(price=10., prev=10.):
    now = datetime.now().replace(microsecond=0)
    hm = now.hour * 60 + now.minute
    if hm >= 900:
        now = now.replace(hour=15, minute=0, second=0)
    elif 690 < hm < 780:
        now = now.replace(hour=11, minute=30, second=0)
    return {"cur": price, "prev": prev, "prev_close": prev, "open": prev,
            "high": max(price, prev) * 1.01, "low": min(price, prev) * .99,
            "vol": 20000, "volume": 20000, "amount": 1e8,
            "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S")}


def position(code="600002", qty=1000, day=None, price=10.):
    return {"code": code, "name": "测试持仓", "buy_price": price,
            "buy_date": day or (date.today() - timedelta(days=30)).isoformat(),
            "quantity": qty, "amount": price * qty, "type": "stock"}


def signal(**kwargs):
    s = {"date": "2026-08-03 10:00", "code": "600003", "name": "测试",
         "grade": "A", "score": 75, "version": "v3.1.0", "industry": "测试",
         "mkt_score": 85, "mkt_state": "A", "new_action": "买入", "action": "买入",
         "price": 10., "quote_time": "2026-08-03 10:00:00", "reduce_ratio": .5}
    s.update(kwargs)
    return s


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {"QQ_SEND_DISABLE": "1", "HOLD_ONLY": "0", "ANALYSIS_ONLY": "0"}))
        self.stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("network disabled")))
        self.stack.enter_context(patch.object(runtime, "DATA_DIR", self.tmp))
        paths = [(dm, "STATE_FILE", "decision.json"), (dm, "HISTORY_FILE", "history.json"),
                 (pm, "POSITIONS_FILE", "positions.json"), (pm, "TRADE_LOG", "trade.csv"),
                 (te, "STATE_FILE", "t.json"), (te, "T_LOG", "t.csv"),
                 (pt, "PAPER_FILE", "paper.json"), (pt, "SIGNAL_LOG", "signals.csv"),
                 (signal_store, "SIGNAL_FILE", "signals.csv"),
                 (watchlist, "WATCHLIST_PATH", "watch.json")]
        for module, key, filename in paths:
            self.stack.enter_context(patch.object(module, key, str(self.tmp / filename)))
        pm.save_positions({"etf": [], "stock": []})
        st.SIGNAL_LOG, st.FINAL_LIST, st.ACTION_LIST = [], [], []
        st.MARKET = {"state": "A", "score": 85, "position": 80, "data_ok": True}
        st.CURRENT_PERIOD = "尾盘"
        st.FILTER_MIN_GRADE = ""
        st.WATCH_STOCKS = []

    def test_shape_gate_cannot_be_overwritten(self):
        kl = bars([10 + i * .05 for i in range(77)] + [14., 15., 16.])
        kl[-1]["volume"] = 5000
        with patch.object(st, "get_rt", return_value=quote(17., 16.)), patch.object(st, "get_kline", return_value=kl):
            st.analyze_item("600001", "过热样本", 0, is_etf=False, bench_chg20=0, bench300_chg20=0)
        self.assertEqual(st.SIGNAL_LOG, [])
        self.assertEqual(st.FINAL_LIST[-1]["action"], "不买")

    def test_t1_hold_has_no_sell_signal_or_summary(self):
        pos = position(day=date.today().isoformat())
        with patch.object(st, "get_rt", return_value=quote(9., 10.)), patch.object(st, "get_kline", return_value=bars([10.] * 80)):
            report = st.analyze_item(pos["code"], "当日买入", 10000, pos=pos, is_etf=False)
        self.assertEqual(st.SIGNAL_LOG, [])
        self.assertEqual(st.ACTION_LIST, [])
        self.assertEqual(st.FINAL_LIST[-1]["action"], "持有")
        self.assertIn("T+1", report)

    def test_mixed_lots_aggregate_and_settle(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        lots = [position(qty=1000, day=yesterday, price=10), position(qty=500, day=date.today().isoformat(), price=12)]
        pos = pm.aggregate_positions({"stock": lots})["600002"]
        self.assertEqual(pos["quantity"], 1500)
        self.assertAlmostEqual(pos["buy_price"], 16000 / 1500)
        r = dm.finalize("600002", "测试", "sell", quality="D", cur=9, pos=pos, hold=16000)
        self.assertEqual(r["can_sell_shares"], 1000)
        self.assertEqual(r["quantity"], 1000)

    def test_profit_protection_stays_activated_after_falling_below_ten_percent(self):
        kl = bars([10.2] * 30)
        kl[-2]["high"] = 11.2
        _, trigs = pm.build_exit_plan(position(day=kl[0]["day"]), 10.25, kl, no_sell=False)
        self.assertIn("盈利保护", [t["kind"] for t in trigs])

    def test_no_sell_applies_to_profit_protection(self):
        r = dm.finalize("600002", "测试", "hold", cur=10.25, pos=position(), hold=10000,
                        exit_triggers=[{"kind": "盈利保护"}], no_sell=True)
        self.assertEqual(r["action"], "HOLD")
        self.assertEqual(r["quantity"], 0)

    def test_preview_does_not_consume_cooldown(self):
        first = dm.finalize("600001", "测试", "buy", quality="S", cur=10, persist=False)
        self.assertEqual(first["action"], "BUY")
        self.assertFalse(Path(dm.STATE_FILE).exists())
        second = dm.finalize("600001", "测试", "buy", quality="S", cur=10)
        self.assertEqual(second["action"], "BUY")

    def test_unknown_market_cannot_buy(self):
        r = dm.finalize("600001", "测试", "buy", quality="S", cur=10, market_state="UNKNOWN")
        self.assertEqual(r["action"], "HOLD")

    def test_corrupt_positions_do_not_become_empty(self):
        Path(pm.POSITIONS_FILE).write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            st.load_positions_map()

    def test_missing_positions_do_not_become_empty(self):
        Path(pm.POSITIONS_FILE).unlink()
        with self.assertRaises(FileNotFoundError):
            st.load_positions_map()

    def test_missing_risk_quote_does_not_understate_assets(self):
        pm.save_positions({"stock": [position()], "etf": []})
        with self.assertRaises(ValueError):
            pm.check_stops({})

    def test_corrupt_decision_state_blocks_decision(self):
        Path(dm.STATE_FILE).write_text("bad", encoding="utf-8")
        with self.assertRaises(ValueError):
            dm.finalize("600001", "测试", "buy", quality="S", cur=10)

    def test_constant_price_has_no_manufactured_t_path(self):
        self.assertEqual(te.calc_buyback_path({"low": 10.}, [{"close": 10.}] * 30, 10., .008), (None, 0))

    def test_nearest_observed_support_must_meet_profit_requirement(self):
        self.assertEqual(te.calc_buyback_path({"low": 9.}, [{"close": 9.99}] * 30, 10., .008), (None, 0))

    def test_overnight_t_keeps_unfinished_buyback(self):
        pos = dict(position("159000", price=1.), type="etf")
        pm.save_positions({"etf": [pos], "stock": []})
        state = te._default_state("159000", "测试")
        state.update(state=te.WAIT_BUYBACK, date="2026-08-01", sell_price=1.2, sell_shares=1000)
        te.save_t_state({"159000": state})
        with patch.object(te, "fetch_quote", return_value=quote(1., 1.)), patch.object(te, "_kline", return_value=bars([1.] * 80)), patch.object(te, "calc_t_score", return_value=(0, {}, {})), patch.object(te, "calc_buyback_score", return_value=(0, {})), patch.object(te, "sector_sync", return_value=(True, "")), patch.object(te, "analyze_dip", return_value=None):
            result = te.analyze("159000")
        self.assertEqual(result["state"], te.WAIT_BUYBACK)
        self.assertTrue(result["pending_warn"])
        self.assertEqual(te.load_t_state()["159000"]["sell_shares"], 1000)

    def test_partial_buyback_preserves_obligation(self):
        state = te._default_state("159000", "测试")
        state.update(state=te.WAIT_BUYBACK, sell_price=1.2, sell_shares=1000)
        te.save_t_state({"159000": state})
        result, _ = te.mark_buyback("159000", 1.1, 400)
        self.assertEqual(result["state"], te.WAIT_BUYBACK)
        self.assertEqual(result["buyback_shares"], 400)
        before = Path(te.STATE_FILE).read_bytes()
        with self.assertRaises(ValueError):
            te.mark_buyback("159000", 1.1, 700)
        self.assertEqual(Path(te.STATE_FILE).read_bytes(), before)
        result, _ = te.mark_buyback("159000", 1.1, 600)
        self.assertEqual(result["state"], te.COMPLETE)

    def test_partial_dip_sell_tracks_remaining(self):
        state = te._default_state("159000", "测试")
        state["dip"].update(state=te.SELL_T, buy_price=1., buy_shares=1000, buy_date="2026-08-01", sell_trigger="目标1半仓", target_sell_shares=500)
        te.save_t_state({"159000": state})
        result, _ = te.mark_dip_sell("159000", 1.02, 300)
        self.assertEqual(result["dip"]["buy_shares"], 700)
        self.assertEqual(result["dip"]["target_sell_shares"], 200)
        self.assertEqual(result["dip"]["state"], te.SELL_T)
        result, _ = te.mark_dip_sell("159000", 1.02, 200)
        self.assertEqual(result["dip"]["buy_shares"], 500)
        self.assertTrue(result["dip"]["target1_done"])
        self.assertEqual(result["dip"]["state"], te.HOLD_T_BUY)

    def test_invalid_fill_does_not_log(self):
        with self.assertRaises(ValueError):
            te.mark_sell("159000", 1., -100)
        self.assertFalse(Path(te.T_LOG).exists())

    def test_macd_against_independent_recursive_reference(self):
        values = [10 + i * .1 + (i % 3) * .2 for i in range(80)]
        fast = slow = values[0]
        signal_line = 0
        for value in values[1:]:
            fast = value * (2 / 13) + fast * (11 / 13)
            slow = value * (2 / 27) + slow * (25 / 27)
            signal_line = (fast - slow) * .2 + signal_line * .8
        dif, dea, hist = runtime.macd(values)
        self.assertAlmostEqual(dif, fast - slow)
        self.assertAlmostEqual(dea, signal_line)
        self.assertAlmostEqual(hist, 2 * (dif - dea))
        self.assertEqual(runtime.macd([10.] * 80), (0., 0., 0.))

    def test_flat_rsi_is_neutral(self):
        self.assertEqual(st.calc_rsi([10.] * 30), 50)

    def test_lot_rounding_never_exceeds_budget(self):
        self.assertEqual(st.round_lot(500, 10), 0)

    def test_pool_lifecycle_counts_dates_not_runs(self):
        entry = {"code": "600010", "name": "测试", "total_score": 90, "stock_score": 90, "industry_score": 90, "industry": "测试", "factor": {"rs": 16, "capital": 19}, "trend": {"above_ma20": True, "above_ma60": True, "ma20_gt_ma60": True}, "position": {"deduct": 0}, "price": 10.}
        today = date.today().isoformat()
        core, _, _ = sp.generate_pool([copy.deepcopy(entry)], "A", None, today)
        again, _, _ = sp.generate_pool([copy.deepcopy(entry)], "A", {"date": today, "core_pool": core}, today)
        self.assertEqual(core[0]["days_in_pool"], again[0]["days_in_pool"])

    def test_ai_rejects_negated_yes_and_unlisted_codes(self):
        self.assertEqual(spa.extract_yes_stocks("测试股份(600010) 允许交易:NO，不是YES"), [])
        with self.assertRaises(ValueError):
            validate_verdict(json.dumps({"decisions": [{"code": "600999", "decision": "YES", "reason": "编造"}]}), [{"code": "600001"}], "A")

    def test_ai_price_must_match_input(self):
        candidate = {"code": "600001", "price": 10., "levels": {"stop": 9.5}}
        verdict = {"decisions": [{"code": "600001", "decision": "YES", "reason": "测试", "entry_ref": "price", "entry": 11., "stop": 9.5}]}
        with self.assertRaises(ValueError):
            validate_verdict(json.dumps(verdict), [candidate], "A")
        verdict["decisions"][0]["entry"] = 10.
        self.assertEqual(validate_verdict(json.dumps(verdict), [candidate], "A")[0][0][0], "600001")

    def test_ai_valid_no_does_not_retry_or_add_conditional_watch(self):
        now = datetime.now()
        pool = {"date": now.strftime("%Y-%m-%d"), "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"), "data_ok": True, "market_status": "A", "market_score": 85, "core_pool": [{"code": "600001", "name": "测试"}]}
        result = {"final": json.dumps({"decisions": [{"code": "600001", "decision": "NO", "reason": "证据不足"}]}), "trend": "", "risk": "", "trader": ""}
        with patch.object(sai.spm, "load_old_pool", return_value=pool), patch.object(sai, "_run_four_roles", return_value=result) as roles, patch.object(spa, "get_market_brief", return_value=""), patch.object(spa, "cleanup_watchlist", return_value=([], set())), patch.object(spa, "add_cond_monitor") as conditional, patch.object(watchlist, "add_stocks") as add, redirect_stdout(io.StringIO()):
            sai.main()
        roles.assert_called_once()
        add.assert_not_called()
        conditional.assert_not_called()

    def test_paper_rejects_b_entry_and_same_day_sell(self):
        paper = pt.fresh_paper()
        self.assertFalse(pe.buy(paper, signal(grade="B"), 10.))
        self.assertTrue(pe.buy(paper, signal(), 10.))
        before = copy.deepcopy(paper)
        self.assertFalse(pe.sell(paper, signal(new_action="清仓"), 10.5))
        self.assertEqual(paper, before)

    def test_paper_reduce_leaves_lots_and_charges_costs(self):
        paper = pt.fresh_paper()
        pe.buy(paper, signal(), 10.)
        bought = paper["positions"]["600003"]["shares"]
        pe.sell(paper, signal(date="2026-08-04 10:00", new_action="减仓", grade="D"), 11.)
        remaining = paper["positions"]["600003"]["shares"]
        self.assertEqual(bought - remaining, int(bought * .5 / 100) * 100)
        self.assertGreater(remaining, 0)
        self.assertEqual(paper["trades"][-1]["grade"], "A")
        self.assertGreater(paper["trades"][-1]["fees"], 0)

    def test_paper_corruption_cannot_reset_capital(self):
        Path(pt.PAPER_FILE).write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            pt.load_paper()

    def test_signal_ledger_dedups_batch_and_paper_replay(self):
        s = signal(name="测试,带逗号")
        signal_store.append_signals([s, s])
        self.assertEqual(len(signal_store.read_signals(signal_store.SIGNAL_FILE)), 1)
        before = Path(signal_store.SIGNAL_FILE).read_bytes()
        paper = pt.fresh_paper()
        self.assertEqual(pt.replay(paper), 1)
        state = copy.deepcopy(paper)
        self.assertEqual(pt.replay(paper), 0)
        self.assertEqual(paper, state)
        self.assertEqual(Path(signal_store.SIGNAL_FILE).read_bytes(), before)

    def test_signal_sell_hold_conflict_is_rejected(self):
        with self.assertRaises(ValueError):
            signal_store.append_signals([signal(action="卖出/减仓", new_action="持有")])

    def test_review_waits_for_five_bars(self):
        sig = signal(date="2026-07-01 10:00")
        kl = bars([10., 11., 12.], end=date(2026, 7, 5))
        with patch.object(review, "get_kline", return_value=kl), patch.object(review, "get_rt") as realtime:
            result = review.pair_trades([sig], {})[0]
        self.assertIsNone(result["result"])
        self.assertFalse(result["closed"])
        realtime.assert_not_called()

    def test_review_extremes_use_same_five_bar_window(self):
        sig = signal(date="2026-07-01 10:00")
        kl = bars([10., 11., 12., 13., 14., 100.], end=date(2026, 7, 8))
        with patch.object(review, "get_kline", return_value=kl):
            result = review.pair_trades([sig], {})[0]
        self.assertEqual(result["result"], 40.)
        self.assertLess(result["max_profit_pct"], 50.)

    def test_incomplete_scanner_is_not_success(self):
        with patch.object(scanner, "_fetch", return_value="null"), patch.object(scanner.time, "sleep"):
            self.assertEqual(scanner.fetch_all_stocks(), [])
        self.assertFalse(scanner.LAST_FETCH_COMPLETE)

    def test_market_missing_components_cannot_return_healthy_grade(self):
        with patch.object(st, "_trend_score", return_value=(30, ["正常"])), patch.object(st, "_breadth_score", return_value=(16, ["数据缺失"])), patch.object(st, "_volume_score", return_value=(20, "正常")), patch.object(st, "_external_score", return_value=(20, ["正常"])):
            st.market_score({"上证指数": {"price": 10}})
        self.assertEqual(st.MARKET["state"], "UNKNOWN")

    def test_quote_date_and_age(self):
        now = datetime(2026, 9, 3, 10, 30)
        self.assertTrue(runtime.quote_is_fresh({"date": "2026-09-03", "time": "10:29:00"}, now))
        self.assertFalse(runtime.quote_is_fresh({"date": "2026-09-02", "time": "10:29:00"}, now))
        self.assertFalse(runtime.quote_is_fresh({"date": "2026-09-03", "time": "10:00:00"}, now))

    def test_existing_process_lock_prevents_state_overwrite(self):
        Path(dm.STATE_FILE + ".lock").write_text("existing", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            dm.finalize("600001", "测试", "buy", quality="S", cur=10)
        self.assertFalse(Path(dm.STATE_FILE).exists())

    def test_partial_qq_delivery_resumes_only_unsent_chunk(self):
        with patch.object(qq_send, "DEFAULT_OPENID", "test-recipient"), patch.object(qq_send, "split_report", return_value=["first", "second"]), patch.object(qq_send, "get_token", return_value="test-token"), patch.object(qq_send, "_send_chunk", side_effect=[True, False]) as send, patch.object(qq_send.time, "sleep"):
            with self.assertRaises(qq_send.PartialDeliveryError):
                qq_send.send_report("test-report")
            self.assertEqual(send.call_count, 2)
            with patch.object(qq_send, "_send_chunk", return_value=True) as retry:
                self.assertTrue(qq_send.send_report("test-report"))
                retry.assert_called_once()
                self.assertIn("second", retry.call_args.args[0])

    def test_ambiguous_qq_response_cannot_automatically_resend(self):
        with patch.object(qq_send, "DEFAULT_OPENID", "test-recipient"), patch.object(qq_send, "get_token", return_value="test-token"), patch.object(qq_send, "_send_chunk", side_effect=TimeoutError) as send:
            with self.assertRaises(qq_send.PartialDeliveryError):
                qq_send.send_report("timeout-report")
            with self.assertRaises(qq_send.PartialDeliveryError):
                qq_send.send_report("timeout-report")
            send.assert_called_once()

    def test_t_fill_and_state_commit_together(self):
        state = te._default_state("159000", "测试")
        state.update(state=te.WAIT_BUYBACK, sell_price=1.2, sell_shares=1000)
        te.save_t_state({"159000": state})
        before = Path(te.STATE_FILE).read_bytes()
        with patch.object(te, "atomic_json", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                te.mark_buyback("159000", 1.1, 400)
        self.assertEqual(Path(te.STATE_FILE).read_bytes(), before)
        self.assertFalse(Path(te.T_LOG).exists())
        te.mark_buyback("159000", 1.1, 400)
        saved = te.load_t_state()
        self.assertEqual(saved["159000"]["buyback_shares"], 400)
        self.assertEqual(saved["__fills__"][0]["数量"], 400)

    def test_new_position_does_not_inherit_previous_peak(self):
        old = position(day="2026-07-01")
        dm.finalize("600002", "测试", "hold", cur=20., pos=old, hold=10000)
        new = position(day="2026-08-01")
        dm.finalize("600002", "测试", "hold", cur=10.5, pos=new, hold=10000)
        self.assertEqual(dm.load_states()["600002"]["highest_price"], 10.5)

    def test_daily_bars_reject_mismatched_price_basis(self):
        with self.assertRaises(ValueError):
            runtime.align_daily_bars(bars([5.] * 80), quote(10., 10.))

    def test_daily_bars_use_current_quote_exactly_once(self):
        q = quote(10.2, 10.)
        history = bars([10.] * 80)
        result = runtime.align_daily_bars(history, q)
        self.assertEqual(result[-1]["close"], 10.2)
        self.assertEqual(runtime.align_daily_bars(result, q), result)

    def test_main_hold_only_covers_all_positions_without_signals(self):
        pm.save_positions({"stock": [position("600099")], "etf": [dict(position("159099", price=1.), type="etf")]})
        st.SIGNAL_LOG = [signal()]
        with patch.dict(os.environ, {"HOLD_ONLY": "1"}), patch.object(st, "ETFS", []), patch.object(st, "EXTRA_ETFS", []), patch.object(st, "STOCKS", []), patch.object(st, "get_indices", return_value={}), patch.object(st, "market_score", return_value=[]), patch.object(st, "get_index_kline", return_value=[]), patch.object(st, "fetch_em_extra"), patch.object(st, "load_stock_pool"), patch.object(st, "load_watch_stocks"), patch.object(st, "get_rt", return_value=quote()), patch.object(pm, "generate_risk_report", return_value=""), patch.object(pm, "generate_signal_stats_report", return_value=""), patch.object(st, "_position_health_report", return_value=""), patch.object(st, "analyze_item", return_value="test") as analyze, redirect_stdout(io.StringIO()):
            st.main()
        self.assertEqual({call.args[0] for call in analyze.call_args_list}, {"600099", "159099"})
        self.assertEqual(st.SIGNAL_LOG, [])
        self.assertFalse(Path(signal_store.SIGNAL_FILE).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
