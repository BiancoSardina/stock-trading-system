"""Immutable pool/decision pairs, published by one atomic manifest replacement."""
import hashlib
import json
import math
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import runtime

NAMES = ("stock_pool.json", "decision_bundle_latest.json")
POINTER = "pool_batch_latest.json"
SCHEMA = "pool-batch/v1"


def batch_dir(batch_id):
    if not re.fullmatch(r"\d{14}_[a-f0-9]{8}", batch_id):
        raise ValueError("Invalid pool batch id")
    return runtime.DATA_DIR / "pool_batches" / batch_id


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_pair(directory, now=None):
    """No partial/mixed/unknown/expired batch can be published or uploaded."""
    now = now or datetime.now()
    directory = Path(directory)
    pool, bundle = [json.loads((directory / name).read_text(encoding="utf-8")) for name in NAMES]
    from decision_bundle import validate_pool
    validate_pool(pool, now)
    if pool.get("data_ok") is not True:
        raise ValueError("Pool integrity must be true")
    if pool.get("market_status") not in ("A", "B", "C", "D"):
        raise ValueError("Unknown pool market")
    if not isinstance(pool.get("market_score"), (int, float)) or not math.isfinite(pool["market_score"]):
        raise ValueError("Missing or invalid market score")
    if pool.get("batch_id"):
        metrics = pool.get("scan_metrics", {})
        if metrics.get("complete") is not True or not isinstance(metrics.get("candidate_count"), int) or \
                metrics.get("processed") != metrics["candidate_count"] or metrics["candidate_count"] < 0:
            raise ValueError("Incomplete candidate scan")
    if bundle.get("schema") != "external-ai-decision-bundle/v1":
        raise ValueError("Invalid decision bundle schema")
    integrity = bundle.get("integrity", {})
    if integrity.get("pool_data_ok") is not True or integrity.get("analysis_only") is not True:
        raise ValueError("Invalid decision bundle integrity")
    if integrity.get("local_ai_called") is not False:
        raise ValueError("Unexpected local AI result")
    for key in ("date", "generated_at", "market_status", "market_score"):
        if key not in pool or bundle.get("market", {}).get(key) != pool[key]:
            raise ValueError("Pool/bundle market metadata mismatch: " + key)
    for group in ("core_pool", "watch_pool"):
        if not isinstance(pool.get(group), list) or bundle.get("stock_pool", {}).get(group) != pool[group]:
            raise ValueError("Pool/bundle constituents mismatch: " + group)
    if pool.get("batch_id") != bundle.get("batch_id"):
        raise ValueError("Pool/bundle batch id mismatch")
    generated = datetime.strptime(bundle["generated_at"], "%Y-%m-%d %H:%M:%S")
    expires = datetime.strptime(bundle["valid_until"], "%Y-%m-%d %H:%M:%S")
    pool_time = datetime.strptime(pool["generated_at"], "%Y-%m-%d %H:%M:%S")
    if not pool_time <= generated <= now <= expires <= generated + timedelta(hours=24):
        raise ValueError("Invalid/expired bundle timestamps")
    return pool, bundle


def current_directory():
    """Resolve both paths from one manifest. Corruption never falls back to old files."""
    pointer = runtime.DATA_DIR / POINTER
    if not pointer.exists():
        return runtime.DATA_DIR
    manifest = json.loads(pointer.read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA:
        raise ValueError("Invalid pool batch manifest")
    directory = batch_dir(manifest["batch_id"])
    for name in NAMES:
        if manifest.get("sha256", {}).get(name) != digest(directory / name):
            raise ValueError("Published batch hash mismatch: " + name)
    return directory


def resolve(name):
    override = os.environ.get("POOL_BATCH_DIR")
    directory = Path(override) if override else current_directory()
    return str(directory / name)


def write_path(name):
    override = os.environ.get("POOL_BATCH_DIR")
    if override:
        directory = Path(override)
        if (directory / "ready.json").exists():
            raise RuntimeError("Published batch is immutable; start a new pipeline run")
        return str(directory / name)
    if (runtime.DATA_DIR / POINTER).exists():
        raise RuntimeError("Use stock_pool_full.py/stock_pool_evening.py to publish a complete batch")
    return str(runtime.DATA_DIR / name)


def publish(batch_id, now=None):
    directory = batch_dir(batch_id)
    pool, _ = validate_pair(directory, now)
    if pool.get("batch_id") != batch_id:
        raise ValueError("Unexpected batch id")
    manifest = {"schema": SCHEMA, "batch_id": batch_id,
                "sha256": {name: digest(directory / name) for name in NAMES}}
    runtime.atomic_json(directory / "ready.json", manifest)
    runtime.atomic_json(runtime.DATA_DIR / POINTER, manifest)
    return manifest


@contextmanager
def pinned_current():
    """Keep report contents and its pool metadata on the same immutable snapshot."""
    previous = os.environ.get("POOL_BATCH_DIR")
    if previous is None:
        os.environ["POOL_BATCH_DIR"] = str(current_directory())
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("POOL_BATCH_DIR", None)
