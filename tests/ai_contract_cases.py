"""A valid NO or already-monitored YES must not trigger directional retries."""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_regressions import RegressionTests
import stock_pool_ai as ai
import stock_picker_ai as spa
import watchlist


class AIDecisionTests(RegressionTests):
    def test_existing_yes_does_not_retry(self):
        now = datetime.now()
        pool = {"date": now.strftime("%Y-%m-%d"), "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "data_ok": True, "market_status": "A", "core_pool": [{"code": "600001", "name": "测试", "price": 10., "levels": {"stop": 9.5}}]}
        answer = {"final": json.dumps({"decisions": [{"code": "600001", "decision": "YES", "reason": "可核验", "entry_ref": "price", "entry": 10., "stop": 9.5}]}), "trend": "", "risk": "", "trader": ""}
        with patch.object(ai.spm, "load_old_pool", return_value=pool), patch.object(ai, "_run_four_roles", return_value=answer) as roles, patch.object(spa, "get_market_brief", return_value=""), patch.object(spa, "cleanup_watchlist", return_value=([], set())), patch.object(watchlist, "add_stocks", return_value=[]) as add, redirect_stdout(io.StringIO()):
            ai.main()
        roles.assert_called_once()
        add.assert_called_once()

    def test_stale_pool_does_not_clean_watchlist(self):
        with patch.object(ai.spm, "load_old_pool", return_value={"date": "2000-01-01"}), patch.object(spa, "cleanup_watchlist") as clean:
            with self.assertRaises(ValueError):
                ai.main()
        clean.assert_not_called()


def run():
    suite = unittest.TestSuite(AIDecisionTests(name) for name in ("test_existing_yes_does_not_retry", "test_stale_pool_does_not_clean_watchlist"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1
