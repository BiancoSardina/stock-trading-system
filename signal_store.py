"""Versioned signal ledger. Historical CSV files are never rewritten."""
import csv
import hashlib
import io
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from runtime import data_path, file_lock, positive

SIGNAL_FILE = data_path("signal_log_v32.csv")
FIELDS = {"时间": "date", "代码": "code", "名称": "name", "操作": "action",
          "价格": "price", "信号等级": "grade", "评分": "score", "行业": "industry",
          "市场评分": "mkt_score", "市场状态": "mkt_state", "状态": "status",
          "策略版本": "version", "旧动作": "old_action", "新动作": "new_action",
          "变化原因": "change_reason", "可执行数量": "quantity", "减仓比例": "reduce_ratio",
          "行情时间": "quote_time", "信号ID": "id",
          "入场止损": "entry_stop", "参考目标": "entry_target", "净盈亏比": "net_rr"}


def signal_id(signal):
    keys = ("date", "code", "new_action", "version")
    return hashlib.sha256(json.dumps([signal.get(k, "") for k in keys], ensure_ascii=False).encode()).hexdigest()[:24]


def valid_signal(s):
    try:
        datetime.strptime(s["date"], "%Y-%m-%d %H:%M")
        if not re.fullmatch(r"\d{6}", s["code"]) or not positive(s["price"]):
            return False
        if s.get("version") not in ("v3.1.0", "v3.2.0") or not 0 <= float(s["score"]) <= 100:
            return False
        if s.get('version') == 'v3.2.0' and s.get('new_action') in ('买入', '加仓'):
            from entry_policy import economics, MIN_NET_RR
            if not positive(s.get('net_rr')) or float(s['net_rr']) < MIN_NET_RR:
                return False
            if not economics(s['code'], s['price'], s.get('entry_stop'), s.get('entry_target'), s.get('quantity'))['allowed']:
                return False
        expected = {"买入": "买入", "加仓": "买入", "减仓": "卖出/减仓", "清仓": "卖出/减仓"}
        return expected.get(s.get("new_action")) == s.get("action")
    except (ValueError, TypeError, KeyError):
        return False


def read_signals(path=SIGNAL_FILE):
    result = []
    if not os.path.exists(path):
        return result
    with open(path, newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if None in row:
                continue
            sig = {key: row.get(column, "") for column, key in FIELDS.items()}
            if not valid_signal(sig):
                continue
            sig["price"] = float(sig["price"])
            sig["id"] = signal_id(sig)
            result.append(sig)
    return result


def append_signals(signals):
    if not signals:
        return
    path = Path(SIGNAL_FILE)
    with file_lock(path):
        # Reject malformed existing ledgers rather than silently dropping rows.
        existing = []
        if path.exists():
            with path.open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                if reader.fieldnames != list(FIELDS):
                    raise ValueError("信号日志表头异常，停止写入")
                existing = list(reader)
                if any(None in row or None in row.values() for row in existing):
                    raise ValueError("信号日志行格式异常，停止写入")
        seen = {r["信号ID"] for r in existing}
        for sig in signals:
            if not valid_signal(sig):
                raise ValueError("最终动作与信号字段不一致")
            sid = signal_id(sig)
            if sid in seen:
                continue
            seen.add(sid)
            existing.append({col: sid if key == "id" else sig.get(key, "") for col, key in FIELDS.items()})
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(FIELDS))
                writer.writeheader()
                writer.writerows(existing)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
