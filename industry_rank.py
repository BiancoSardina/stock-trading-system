#!/usr/bin/env python3
"""
股票池V1.1 — 第二层：行业强度（industry_rank）
=============================================
数据源（实测校准，见 stock_pool_design_v1.md §3）：
  · 行业列表：新浪 getHQNodes（49个新浪行业，new_XX 节点编码）
  · 行业成分：新浪 getHQNodeData?node=new_XX（num上限100/页，分页）
  · 5日/20日涨幅：行业代表股（全成分成交额前3）K线聚合 —— push2his被封后的
    稳定替代方案（东财 f109/f110 缓存为可选增强，暂不实现）

评分模型（V1.1 修改三）：
  行业评分 = 涨幅30 + 成交额20 + 趋势30 + 赚钱效应20
  · 涨幅30：代表股周期涨幅(5日×0.4+20日×0.6)排名：前10%=30/前30%=25/中间=15/弱=0
  · 成交额20：成分成交额总和排名：前10%=20/前30%=16/中间=10/弱=4
  · 趋势30：三要件近似（push2his被封无法算指数MA）：
      要件1 "指数>MA20" ≈ 5日涨幅>0 (+10)
      要件2 "MA20>MA60" ≈ 20日涨幅>0 (+10)
      要件3 "MA60向上"  ≈ 5日与20日均>0 双重确认 (+10)
  · 赚钱效应20：成分上涨比例 ≥80%=20 / 60-80%=15 / 40-60%=10 / <40%=0
保留规则：industry_score ≥70 才进入下一层
"""
import json
import os
import sys
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SINA_NODES = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "Market_Center.getHQNodes")
SINA_HQ = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "Market_Center.getHQNodeData")
SINA_KLINE = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "CN_MarketData.getKLineData")
INDUSTRY_MAP_PATH = os.path.join(SCRIPT_DIR, "industry_map.json")
REP_STOCKS_N = 3          # 每行业代表股数（成交额前N）
INDUSTRY_KEEP = 70        # 行业评分保留线


def _fetch(url, retries=3, timeout=25):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(3 * (i + 1))
    return ""


def _get_prefix(code):
    return "sh" if code.startswith(("60", "68")) else "sz"


def get_kline(code, days=30):
    """新浪日K线（quotes.sina.cn 端点，抗456限流；25根够算5日/20日）"""
    pref = "sh" if code.startswith(("60", "68")) else "sz"
    url = (f"https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
           f"?symbol={pref}{code}&scale=240&ma=no&datalen={days}")
    raw = _fetch(url)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def fetch_industry_list():
    """新浪行业列表：[(行业名, node), ...]（约49个）"""
    raw = _fetch(SINA_NODES)
    if not raw:
        return []
    try:
        tree = json.loads(raw)
    except json.JSONDecodeError:
        return []
    industries = []

    def walk(node):
        if isinstance(node, list) and len(node) >= 2 and isinstance(node[1], list):
            for sub in node[1]:
                if (isinstance(sub, list) and len(sub) >= 3
                        and isinstance(sub[0], str) and isinstance(sub[2], str)
                        and sub[2].startswith("new_")):
                    industries.append((sub[0], sub[2]))
                else:
                    walk(sub)
    walk(tree)
    return industries


def fetch_industry_members(node, max_page=3):
    """拉单个行业成分：返回 [{code, name, chg, amount}, ...]（分页）"""
    out = []
    for pn in range(1, max_page + 1):
        url = (f"{SINA_HQ}?page={pn}&num=100&sort=amount&asc=0&node={node}")
        try:
            raw = _fetch(url)
        except Exception:
            break
        if not raw or raw.strip() == "null":
            break
        try:
            arr = json.loads(raw)
        except json.JSONDecodeError:
            break
        if not arr:
            break
        for s in arr:
            try:
                out.append({
                    "code": str(s["code"]), "name": str(s.get("name", "")),
                    "chg": float(s.get("changepercent", 0) or 0),
                    "amount": float(s.get("amount", 0) or 0),
                })
            except (KeyError, TypeError, ValueError):
                continue
        if len(arr) < 100:
            break
        time.sleep(0.25)
    return out


def build_industry_map(force=False):
    """
    全量建行业映射表 → industry_map.json
    {"updated": "...", "industries": {"玻璃行业": {"node":"new_blhy", "total":19,
      "stocks":["600176",...], "reps":["600176",...]}}}
    force=True 强制重建；否则若当天已建直接读缓存

    V2.0 容错（2026-08-18 修复）：
      ① 单行业拉取失败/为空 → 立即重试2次，仍失败记入 failed 列表（不再静默丢失）
      ② 行业数 < 40（设计约49）→ 判数据拉取不全：告警 + 回退最近一次更完整的缓存
      ③ 拉取统计输出 stderr（供晚间任务数据校验）
    """
    if not force:
        try:
            with open(INDUSTRY_MAP_PATH, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("updated") == time.strftime("%Y-%m-%d") and old.get("industries"):
                return old
        except Exception:
            pass
    industries_list = fetch_industry_list()
    if not industries_list:
        print("[industry_rank] ❌ 行业列表拉取失败（getHQNodes 无数据）", file=sys.stderr)
        return load_industry_map()
    industries = {}
    failed = []
    for name, node in industries_list:
        members = None
        for attempt in range(3):  # 单行业最多3次尝试
            try:
                members = fetch_industry_members(node)
            except Exception:
                members = None
            if members:
                break
            time.sleep(2 * (attempt + 1))
        if not members:
            failed.append(name)
            continue
        stocks = [m["code"] for m in members]
        # 代表股：全成分按成交额前N（反映行业强弱，不限主板）
        reps = [m["code"] for m in sorted(members, key=lambda x: -x["amount"])[:REP_STOCKS_N]]
        industries[name] = {"node": node, "total": len(stocks), "stocks": stocks, "reps": reps}
        time.sleep(0.3)
    # V2.0 完整性校验：行业数明显不足 → 回退旧缓存（防限流期把完整缓存覆盖成残缺版）
    if len(industries) < 40 and failed:
        try:
            with open(INDUSTRY_MAP_PATH, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("industries") and len(old["industries"]) > len(industries):
                print(f"[industry_rank] ⚠️ 行业映射拉取不全 {len(industries)}/{len(industries_list)}"
                      f"（失败: {failed[:8]}），回退旧缓存 {len(old['industries'])}个({old.get('updated')})",
                      file=sys.stderr)
                return old
        except Exception:
            pass
    if failed:
        print(f"[industry_rank] ⚠️ 行业映射 {len(industries)}/{len(industries_list)}，"
              f"失败行业: {failed[:10]}{'...' if len(failed) > 10 else ''}", file=sys.stderr)
    else:
        print(f"[industry_rank] ✅ 行业映射完整 {len(industries)}/{len(industries_list)}", file=sys.stderr)
    data = {"updated": time.strftime("%Y-%m-%d"), "industries": industries}
    with open(INDUSTRY_MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def load_industry_map():
    try:
        with open(INDUSTRY_MAP_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"updated": "", "industries": {}}


def score_industries(all_stocks, industry_map):
    """
    行业强度评分（V1.1 修改三模型）。
    all_stocks: 全市场列表（含 code/chg/amount，来自 stock_scanner.fetch_all_stocks）
    返回 {行业名: {score, chg, amount, up_ratio, period, detail...}}
    """
    industries = industry_map.get("industries", {})
    # 全市场索引（成分数据匹配用）
    idx = {}
    for s in all_stocks:
        idx[str(s.get("code", ""))] = s

    rows = []
    for name, info in industries.items():
        members = []
        for c in info.get("stocks", []):
            m = idx.get(c)
            if m:
                members.append(m)
        if not members:
            continue
        # 注意：all_stocks 来自 Market_Center（字段名 changepercent/amount），
        # 不是 fetch_industry_members 的 chg 命名
        avg_chg = sum(float(m.get("changepercent", 0) or 0) for m in members) / len(members)
        tot_amount = sum(float(m.get("amount", 0) or 0) for m in members)
        up_n = sum(1 for m in members if float(m.get("changepercent", 0) or 0) > 0)
        up_ratio = up_n / len(members)
        rows.append({
            "name": name, "avg_chg": avg_chg, "amount": tot_amount,
            "up_ratio": up_ratio, "n": len(members),
            "reps": info.get("reps", []),
        })

    if not rows:
        return {}
    rows.sort(key=lambda x: -x["avg_chg"])
    n = len(rows)

    def rank_pct(i):
        return (i + 1) / n

    # 代表股 5日/20日 涨幅（K线聚合，约 49×3=147 次请求）
    rep_period = {}
    for r in rows:
        closes_list = []
        for code in r["reps"]:
            kl = get_kline(code, 30)
            if len(kl) >= 21:
                closes_list.append([float(k["close"]) for k in kl])
            time.sleep(0.15)
        chg5 = chg20 = None
        if closes_list:
            # 各代表股涨幅均值
            c5 = [round((c[-1] / c[-6] - 1) * 100, 2) for c in closes_list if len(c) >= 6]
            c20 = [round((c[-1] / c[-21] - 1) * 100, 2) for c in closes_list if len(c) >= 21]
            if c5:
                chg5 = sum(c5) / len(c5)
            if c20:
                chg20 = sum(c20) / len(c20)
        rep_period[r["name"]] = (chg5, chg20)

    # 周期强度排序（5日×0.4 + 20日×0.6，缺失用当日涨幅折算）
    def period_strength(name):
        c5, c20 = rep_period.get(name, (None, None))
        if c5 is not None and c20 is not None:
            return c5 * 0.4 + c20 * 0.6
        base = next(r["avg_chg"] for r in rows if r["name"] == name)
        return base * 0.5  # 降级：当日涨幅折算

    scored = {}
    for i, r in enumerate(rows):
        # ① 涨幅30：周期强度排名
        pct = rank_pct(i)
        s_period = 30 if pct <= 0.10 else 25 if pct <= 0.30 else 15 if pct <= 0.70 else 0
        # ② 成交额20
        amt_sorted = sorted(rows, key=lambda x: -x["amount"])
        amt_rank = amt_sorted.index(r) / n
        s_amt = 20 if amt_rank <= 0.10 else 16 if amt_rank <= 0.30 else 10 if amt_rank <= 0.70 else 4
        # ③ 趋势30：三要件近似
        c5, c20 = rep_period.get(r["name"], (None, None))
        s_trend = 0
        t_parts = []
        if c5 is not None and c5 > 0:
            s_trend += 10
            t_parts.append("5日↑")
        if c20 is not None and c20 > 0:
            s_trend += 10
            t_parts.append("20日↑")
        if (c5 is not None and c5 > 0) and (c20 is not None and c20 > 0):
            s_trend += 10
            t_parts.append("双确认")
        # ④ 赚钱效应20
        ur = r["up_ratio"]
        s_up = 20 if ur >= 0.80 else 15 if ur >= 0.60 else 10 if ur >= 0.40 else 0
        score = s_period + s_amt + s_trend + s_up
        scored[r["name"]] = {
            "score": score, "avg_chg": round(r["avg_chg"], 2),
            "amount": r["amount"], "up_ratio": round(ur, 3), "n": r["n"],
            "period": (round(c5, 2) if c5 is not None else None,
                       round(c20, 2) if c20 is not None else None),
            "detail": f"涨幅{s_period}+成交{s_amt}+趋势{s_trend}({','.join(t_parts) or '无'})+赚钱{s_up}",
        }
    return scored


def stock_industry_index(industry_map):
    """股票代码 → 行业名 反查索引"""
    index = {}
    for name, info in industry_map.get("industries", {}).items():
        for c in info.get("stocks", []):
            index[c] = name
    return index


if __name__ == "__main__":
    import sys
    sys.path.insert(0, SCRIPT_DIR)
    import stock_scanner
    t0 = time.time()
    print("[industry_rank] 建行业映射表...")
    imap = build_industry_map(force=False)
    print(f"[industry_rank] 行业数: {len(imap['industries'])} 耗时{time.time()-t0:.0f}s")
    all_s = stock_scanner.fetch_all_stocks()
    t1 = time.time()
    print(f"[industry_rank] 全市场 {len(all_s)}只 耗时{t1-t0:.0f}s，开始评分...")
    sc = score_industries(all_s, imap)
    print(f"[industry_rank] 评分完成 耗时{time.time()-t1:.0f}s")
    top = sorted(sc.items(), key=lambda x: -x[1]["score"])[:12]
    for name, d in top:
        print(f"  {name}: {d['score']}分 当日{d['avg_chg']:+.2f}% 上涨率{d['up_ratio']*100:.0f}% "
              f"5日/20日{d['period']} | {d['detail']}")
    print("...")
    weak = sorted(sc.items(), key=lambda x: x[1]["score"])[:5]
    for name, d in weak:
        print(f"  {name}: {d['score']}分 (弱)")
