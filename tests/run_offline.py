"""Run supported tests in a temporary source/data copy with networking disabled."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TESTS = ["test_decision_manager.py", "test_stock_pool_v12.py", "test_pool_gate_v132.py",
         "test_regressions.py", "test_entry_policy.py", "test_rebound_pool.py",
         "test_decision_bundle.py"]

with tempfile.TemporaryDirectory(prefix="stock_regression_") as temp:
    target = Path(temp)
    for source in ROOT.rglob("*.py"):
        path = target / source.relative_to(ROOT)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, path)
    data = target / "runtime_data"
    data.mkdir()
    (data / "positions.json").write_text('{"stock": [], "etf": []}', encoding="utf-8")
    env = dict(os.environ, STOCK_DATA_DIR=str(data), PYTHONUTF8="1", QQ_SEND_DISABLE="1",
               PYTHONPATH=str(target), ANALYSIS_ONLY="0", HOLD_ONLY="0")
    runner = """import runpy, sys, socket, urllib.request
def blocked(*a, **kw):
    raise RuntimeError('offline test network disabled')
urllib.request.urlopen = blocked
socket.create_connection = blocked
sys.argv = [sys.argv[1]]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    failures = []
    for name in TESTS:
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", runner, str(target / "tests" / name)],
                                cwd=target, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        print(f"{'PASS' if result.returncode == 0 else 'FAIL'} {name}")
        if result.returncode:
            print(result.stdout)
            print(result.stderr)
            failures.append(name)
        else:
            print("\n".join((result.stdout + result.stderr).splitlines()[-4:]))
    if failures:
        raise SystemExit(1)
    print("All offline suites passed; production data and network were not used.")
