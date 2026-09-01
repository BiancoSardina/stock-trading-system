#!/usr/bin/env python3
"""
watchlist.py — 次日监测名单管理（2026-08-05 用户需求）

机制：
  1. 18:00 选股任务(stock_picker_ai.py) 把 AI 裁决"允许交易 YES"的标的写入 watchlist.json
  2. 次日盘中任务(short_term.py) 读取该名单，把这些个股加入分析并重点监测
  3. 次日 18:00 任务运行时清理：检查 positions.json——
     买入了 → 转入持仓监控，移出名单；没买入 → 停止监测，移出名单

文件：~/.hermes/scripts/watchlist.json
结构：{"stocks": [{"code","name","added","reason"}], "updated": "YYYY-MM-DD"}
"""
import json
import os
from datetime import datetime

WATCHLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")


def load_watchlist() -> list:
    """读取监测名单，返回 [{code, name, added, reason}]，异常返回空列表"""
    try:
        with open(WATCHLIST_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("stocks", []) or []
    except Exception:
        return []


def save_watchlist(entries: list) -> bool:
    """保存监测名单，成功返回 True"""
    try:
        data = {
            "stocks": entries,
            "updated": datetime.now().strftime("%Y-%m-%d"),
        }
        with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def clean_stale(positions_codes: set, today: str) -> dict:
    """
    清理过期待监测标的（added < today 即前一天或更早加入的）：
      - 已买入（code 在持仓中）→ 移出名单，标记 bought
      - 未买入 → 移出名单，标记 expired
    返回 {"bought": [...], "expired": [...], "kept": [...]}
    """
    entries = load_watchlist()
    kept, bought, expired = [], [], []
    for e in entries:
        added = str(e.get("added", ""))
        if added and added < today:
            if e.get("code") in positions_codes:
                bought.append(e)
            else:
                expired.append(e)
        else:
            kept.append(e)
    save_watchlist(kept)
    return {"bought": bought, "expired": expired, "kept": kept}


def add_stocks(codes_names: list, reason: str = "18:00选股允许交易YES") -> list:
    """
    把新标的加入监测名单（按 code 去重）。
    codes_names: [(code, name), ...]
    返回本次实际新增的条目列表
    """
    entries = load_watchlist()
    existing = {e.get("code") for e in entries}
    today = datetime.now().strftime("%Y-%m-%d")
    added = []
    for code, name in codes_names:
        if code in existing:
            continue
        item = {"code": code, "name": name, "added": today, "reason": reason}
        entries.append(item)
        existing.add(code)
        added.append(item)
    save_watchlist(entries)
    return added


def summary() -> str:
    """返回当前名单的展示文本（供报告输出）"""
    entries = load_watchlist()
    if not entries:
        return "监测名单为空"
    lines = [f"{e.get('code')} {e.get('name')} (加入{e.get('added')})" for e in entries]
    return "；".join(lines)
