import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import entry_policy as ep
import decision_manager as dm
import signal_store
import paper_execution as pe
from report_contract import BASE_PROMPT


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for mod, name, value in [(dm, 'STATE_FILE', str(self.root/'states.json')),
                                 (dm, 'HISTORY_FILE', str(self.root/'history.json'))]:
            p = patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, ANALYSIS_ONLY='0', HOLD_ONLY='0')
        p.start(); self.addCleanup(p.stop)
        self.plan = {'entry_low':9.9,'entry_high':10.04,'support':9.9,'stop':9.6,'target':12}
        self.quote = {'cur':10,'open':9.95,'prev':9.98}
        self.clock = datetime(2026,9,3,14,47)

    def test_small_etf_upside_is_consumed_by_fees(self):
        x = ep.economics('159532',1.659,1.600,1.679,300)
        self.assertFalse(x['allowed'])
        self.assertLess(x['net_profit'],0)

    def test_target_below_entry_cannot_be_a_profit_target(self):
        self.assertFalse(ep.economics('159532',1.659,1.60,1.645,10000)['allowed'])

    def test_valid_opportunity_can_pass(self):
        x=ep.assess('600001',self.quote,self.plan,1000,'A',False,ma5=9.99,now=self.clock)
        self.assertTrue(x['allowed'],x)

    def test_a_stock_requires_recovery_even_with_high_rr(self):
        x=ep.assess('600001',self.quote,self.plan,1000,'A',False,ma5=10.1,now=self.clock)
        self.assertFalse(x['allowed'])
        self.assertTrue(x['economics']['allowed'])

    def test_close_does_not_override_entry_zone(self):
        x=ep.assess('600001',dict(self.quote,cur=10.5),self.plan,1000,'S',False,now=self.clock)
        self.assertFalse(x['allowed'])
        self.assertTrue(any('买入区' in s for s in x['reasons']))

    def test_support_is_not_moved_up_to_chase(self):
        bars=[{'day':f'2026-08-{d:02d}','low':8,'high':10,'close':9} for d in range(24,29)]
        p=ep.make_plan(15,8.5,bars,today='2026-09-03')
        self.assertEqual(p['support'],8.5)
        self.assertEqual(p['target'],10)

    def test_future_and_incomplete_bars_do_not_change_plan(self):
        bars=[{'day':f'2026-08-{d:02d}','low':8,'high':10,'close':9} for d in range(24,29)]
        p=ep.make_plan(9,8.5,bars,today='2026-09-03')
        bars.extend([{'day':d,'low':1,'high':99,'close':20} for d in ('2026-09-03','2026-09-04')])
        self.assertEqual(p,ep.make_plan(9,8.5,bars,today='2026-09-03'))

    def test_after_hours_are_plans_only(self):
        x=ep.assess('600001',self.quote,self.plan,1000,'S',False,now=datetime(2026,9,3,15,1))
        self.assertFalse(x['allowed'])

    def test_invalid_numerics_fail_closed(self):
        for val in (None,float('nan'),float('inf'),-1,0):
            self.assertFalse(ep.economics('600001',val,9,12,1000)['allowed'])

    def test_blocked_entry_cannot_override_hard_exit(self):
        pos={'code':'600001','buy_price':10,'quantity':1000,'buy_date':'2026-08-01'}
        r=dm.finalize('600001','测试','buy',quality='S',cur=9,pos=pos,
            entry_check={'allowed':False,'reasons':['费用不通过']},exit_triggers=[{'kind':'止损退出'}],now=self.clock)
        self.assertEqual(r['action'],'SELL')
        self.assertEqual(r['quantity'],1000)

    def test_stop_memory_survives_empty_position_and_weekend(self):
        pos={'code':'600001','buy_price':10,'quantity':1000,'buy_date':'2026-08-01'}
        dm.finalize('600001','测试','sell',cur=9,pos=pos,exit_triggers=[{'kind':'止损退出'}],now=datetime(2026,8,14,10))
        a=dm.finalize('600001','测试','buy',quality='S',cur=10,now=datetime(2026,8,17,10),completed_dates=['2026-08-14'])
        self.assertEqual(a['action'],'HOLD')
        b=dm.finalize('600001','测试','buy',quality='S',cur=10,now=datetime(2026,8,18,10),completed_dates=['2026-08-14','2026-08-17'])
        self.assertEqual(b['action'],'HOLD')
        c=dm.finalize('600001','测试','buy',quality='S',cur=10,now=datetime(2026,8,19,10),completed_dates=['2026-08-14','2026-08-17','2026-08-18'])
        self.assertEqual(c['action'],'BUY')
        self.assertEqual(dm.load_states()['600001']['last_stop_signal_date'],'2026-08-14')

    def test_preview_does_not_write_stop_memory(self):
        pos={'code':'600001','buy_price':10,'quantity':1000,'buy_date':'2026-08-01'}
        dm.finalize('600001','测试','sell',cur=9,pos=pos,exit_triggers=[{'kind':'止损退出'}],persist=False,now=self.clock)
        self.assertFalse(Path(dm.STATE_FILE).exists())

    def test_missing_session_history_does_not_release_stop_cooldown(self):
        self.assertIsNotNone(ep.cooldown_reason('2026-08-14',None,'2026-08-25'))

    def test_rejected_entry_has_zero_quantity_and_no_history(self):
        r=dm.finalize('600001','测试','buy',quality='S',cur=10,requested_buy_shares=1000,
            entry_check={'allowed':False,'reasons':['不在买区']},now=self.clock)
        self.assertEqual((r['action'],r['quantity']),('HOLD',0))
        self.assertFalse(Path(dm.HISTORY_FILE).exists())

    def test_v32_signal_cannot_omit_entry_evidence(self):
        s={'date':'2026-09-03 14:47','code':'600001','price':10,'score':85,'version':'v3.2.0','action':'买入','new_action':'买入','quantity':1000}
        self.assertFalse(signal_store.valid_signal(s))
        s.update(entry_stop=9.6,entry_target=12,net_rr=ep.economics('600001',10,9.6,12,1000)['net_rr'])
        self.assertTrue(signal_store.valid_signal(s))

    def test_paper_respects_qualified_order_quantity(self):
        paper={'cash':200000,'positions':{},'trades':[]}
        sig={'code':'600001','name':'测试','new_action':'买入','grade':'S','mkt_state':'A','date':'2026-09-03 14:47','version':'v3.2.0','quantity':100,'entry_stop':9.6,'entry_target':12}
        self.assertTrue(pe.buy(paper,sig,10))
        self.assertEqual(paper['positions']['600001']['shares'],100)

    def test_paper_rechecks_economics_after_budget_reduces_size(self):
        paper={'cash':200,'positions':{},'trades':[]}
        sig={'code':'159532','new_action':'买入','grade':'S','mkt_state':'A','date':'2026-09-03 14:47','version':'v3.2.0','quantity':10000,'entry_stop':1.6,'entry_target':1.7}
        self.assertFalse(pe.buy(paper,sig,1.659))
        self.assertEqual(paper['trades'],[])

    def test_report_contract_does_not_force_end_of_day_buys(self):
        self.assertIn('尾盘也不强制买入',BASE_PROMPT)
        self.assertIn('不得生成条件买单绕过拦截',BASE_PROMPT)


if __name__=='__main__':
    unittest.main()
