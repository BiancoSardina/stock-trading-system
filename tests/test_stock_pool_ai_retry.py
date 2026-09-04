"""Regression entrypoint: valid verdicts no longer trigger directional retries."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ai_contract_cases import run
if __name__ == "__main__":
    raise SystemExit(run())
