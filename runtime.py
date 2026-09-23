"""Shared data location, atomic writes and process-level transaction locks."""
import functools
import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("STOCK_DATA_DIR", Path(__file__).resolve().parent))

# 跨链公共发布锁：池链与分析链共用同一把锁文件，串行化对同一 git 工作区的发布动作。
PUBLISH_GUARD = "publish.guard"
PUBLISH_LOCK_HELD_ENV = "PUBLISH_LOCK_HELD"


def data_path(name):
    if name in ("stock_pool.json", "decision_bundle_latest.json"):
        from pool_batch import resolve
        return resolve(name)
    # Killed pipeline children may leave lock files. Keep those locks isolated
    # inside their unique batch; the parent holds the cross-run OS lock.
    if name.endswith(".run") and os.environ.get("POOL_BATCH_DIR"):
        return str(Path(os.environ["POOL_BATCH_DIR"]) / name)
    return str(DATA_DIR / name)


def analysis_only():
    return os.environ.get("ANALYSIS_ONLY") == "1" or os.environ.get("HOLD_ONLY") == "1"


def restore_analysis_mode(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        previous = os.environ.get("ANALYSIS_ONLY")
        try:
            return fn(*args, **kwargs)
        finally:
            if previous is None:
                os.environ.pop("ANALYSIS_ONLY", None)
            else:
                os.environ["ANALYSIS_ONLY"] = previous
    return wrapped


def atomic_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(obj, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default
    # Corruption must never silently reset positions or account balances.


@contextmanager
def file_lock(path):
    lock = str(path) + ".lock"
    Path(lock).parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f"任务正在运行或遗留锁需核对：{lock}") from exc
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield
    finally:
        os.unlink(lock)


def _lock_stream(stream):
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_stream(stream):
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def publish_lock(wait=None):
    """跨链公共发布锁：池链（整轮）与分析链（git 段）共用同一把 flock。

    · 已持有锁的父流程给子进程传 PUBLISH_LOCK_HELD=1，子进程直接放行（避免自锁）。
    · 默认非阻塞：抢不到立即 OSError（消息说明占用者与等待开关）；PUBLISH_LOCK_WAIT=<秒> 改为等待重试。
    """
    if os.environ.get(PUBLISH_LOCK_HELD_ENV) == "1":
        yield
        return
    if wait is None:
        try:
            wait = float(os.environ.get("PUBLISH_LOCK_WAIT", "0"))
        except (TypeError, ValueError):
            wait = 0.0
    path = DATA_DIR / PUBLISH_GUARD
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            try:
                _lock_stream(stream)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise OSError(f"发布锁被占用：{path}；另一条发布流程正在运行"
                                  f"（PUBLISH_LOCK_WAIT=<秒> 可改为等待）") from exc
                time.sleep(0.5)
        try:
            yield
        finally:
            stream.seek(0)
            _unlock_stream(stream)


def exclusive(path_getter):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with file_lock(path_getter()):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def positive(value):
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (ValueError, TypeError):
        return False


def lot_quantity(pos):
    value = pos.get("shares", pos.get("quantity"))
    if value is None:
        price = float(pos.get("buy_price") or 0)
        value = round(float(pos.get("amount") or 0) / price) if price > 0 else 0
    if not math.isfinite(float(value)) or float(value) < 0 or int(float(value)) != float(value):
        raise ValueError("持仓数量必须是非负整数")
    return int(float(value))


def available_quantity(pos, today=None):
    today = today or datetime.now().strftime("%Y-%m-%d")
    # Undated and future-dated lots are unavailable until reconciled.
    return sum(lot_quantity(p) for p in pos.get("lots", [pos])
               if p.get("buy_date") and str(p["buy_date"])[:10] < today)


def ema(values, period):
    if not values:
        return []
    result = [float(values[0])]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        result.append(alpha * float(value) + (1 - alpha) * result[-1])
    return result


def macd(values):
    fast, slow = ema(values, 12), ema(values, 26)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = ema(dif, 9)
    return (dif[-1], dea[-1], 2 * (dif[-1] - dea[-1])) if dif else (None, None, None)


def quote_is_fresh(quote, now=None):
    now = now or datetime.now()
    try:
        quoted = datetime.strptime(quote.get("date", "") + " " + quote.get("time", ""), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return False
    if quoted.date() != now.date() or quoted > now:
        return False
    hm = now.hour * 60 + now.minute
    if hm >= 15 * 60:
        return quoted.hour * 60 + quoted.minute >= 14 * 60 + 55
    if 11 * 60 + 30 < hm < 13 * 60:
        return quoted.hour * 60 + quoted.minute >= 11 * 60 + 25
    return (now - quoted).total_seconds() <= 300


def weekly_averages(kline):
    weeks = {}
    for bar in kline:
        try:
            day = datetime.strptime(str(bar["day"])[:10], "%Y-%m-%d")
            week = day.isocalendar()[:2]
            weeks[week] = float(bar["close"])
        except (KeyError, TypeError, ValueError):
            return None, None
    closes = [weeks[key] for key in sorted(weeks)]
    return tuple(sum(closes[-n:]) / n if len(closes) >= n else None for n in (5, 10))


def align_daily_bars(kline, quote):
    """Validate date order and prior-close basis, then use one current-day quote."""
    if not isinstance(kline, list) or not kline:
        raise ValueError("日K为空")
    today = datetime.strptime(quote["date"], "%Y-%m-%d").date()
    days = [datetime.strptime(str(k["day"])[:10], "%Y-%m-%d").date() for k in kline]
    if days != sorted(set(days)) or days[-1] > today or (today - days[-1]).days > 7:
        raise ValueError("日K重复、乱序、未来或过期")
    history = list(kline[:-1] if days[-1] == today else kline)
    previous = float(quote.get("prev", quote.get("prev_close", 0)))
    if not positive(previous) or not history or not positive(history[-1].get("close")):
        raise ValueError("缺少有效前收盘价")
    if abs(float(history[-1]["close"]) / previous - 1) > .01:
        raise ValueError("日K与报价前收盘价不一致，需核验复权或缺失交易日")
    current = {"day": quote["date"], "open": quote["open"], "close": quote["cur"],
               "high": quote["high"], "low": quote["low"],
               "volume": quote.get("vol", quote.get("volume", 0))}
    return history + [current]


def position_key(pos):
    return sorted({(str(p.get("buy_date", "")), float(p.get("buy_price") or 0))
                   for p in pos.get("lots", [pos])}) if pos else []
