"""Same-day cache of completed daily bars. Live quotes are never cached here."""
from datetime import datetime
import os
from pathlib import Path
import re

from runtime import atomic_json, data_path, positive, read_json
from startup_policy import completed_bars

STATS = {"hits": 0, "misses": 0, "write_failures": 0}


def _history(rows, today, previous):
    if not isinstance(rows, list) or not rows:
        return []
    dates = [str(row.get("day", ""))[:10] for row in rows]
    if dates != sorted(set(dates)) or dates[-1] > today:
        return []
    bars = completed_bars(rows, today)
    if len(bars) < 60 or (datetime.strptime(today, "%Y-%m-%d") -
                          datetime.strptime(bars[-1]["day"], "%Y-%m-%d")).days > 7:
        return []
    if not positive(previous) or abs(bars[-1]["close"] / float(previous) - 1) > .01:
        return []
    return bars


def get(code, days, quote, loader, now=None):
    """Quote's prior-close basis must still match cached bars (e.g. ex-rights)."""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    if os.environ.get("DAILY_HISTORY_CACHE", "1") == "0" or quote.get("date") != today:
        return loader()
    if not re.fullmatch(r"\d{6}", code) or not isinstance(days, int) or days < 60:
        return loader()
    path = Path(data_path("daily_history")) / today / f"{code}_{days}.json"
    try:
        cached = read_json(path, {})
        bars = _history(cached.get("bars"), today, quote.get("prev"))
        if cached.get("schema") == "completed-daily/v1" and cached.get("fetched_on") == today and bars:
            STATS["hits"] += 1
            return bars
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        pass
    STATS["misses"] += 1
    rows = loader()
    try:
        bars = _history(rows, today, quote.get("prev"))
    except (ValueError, TypeError, AttributeError, KeyError):
        bars = []
    if bars:
        try:
            atomic_json(path, {"schema": "completed-daily/v1", "fetched_on": today, "bars": bars})
        except OSError:
            STATS["write_failures"] += 1
    return rows
