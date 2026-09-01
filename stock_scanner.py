#!/usr/bin/env python3
"""
股票池V1.1 — 第一层：全市场拉取 + 基础过滤（stock_scanner）
============================================================
数据源：新浪 Market_Center（实测 26s 拉全市场 5538 只，稳定）
权限硬条件：只做沪深主板 600/601/603/605/000/001/002/003
           （用户未开通创业板300/301、科创板688/689、北交所）

设计依据：~/.hermes/scripts/stock_pool_design_v1.md §4.1 + V1.1修改四
"""
import json
import time
import urllib.request

SINA_HQ = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "Market_Center.getHQNodeData")
MAIN_BOARD_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
MIN_PRICE = 5.0            # 股价门槛
MIN_AMOUNT = 2e8           # 成交额门槛 ≥2亿（V1.1 修改四：原5亿→2亿，当日近似）
LIMIT_DOWN = -9.9          # 当天跌停线（ST已剔除，主板非ST跌停=-10%）
PAGE_SIZE = 100
MAX_PAGE = 70              # 全市场 hs_a 约 56 页


def _fetch(url, retries=3, timeout=25):
    """新浪接口请求（带重试）"""
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


def fetch_all_stocks(max_page=MAX_PAGE):
    """拉取全市场A股（新浪 node=hs_a，含北交所，后续过滤）"""
    out = []
    pn = 1
    while pn <= max_page:
        url = (f"{SINA_HQ}?page={pn}&num={PAGE_SIZE}&sort=symbol&asc=1"
               f"&node=hs_a&symbol=&_s_r_a=init")
        raw = None
        # 2026-08-26 修复：晚间任务 S5 首板池连续两天 0只——行业映射等大量请求后
        # 新浪接口限流，首页返回 null 直接 break → 全市场误判失败。
        # 每页最多重试3次（2s/4s 退避），首页尤其关键。
        for attempt in range(3):
            try:
                raw = _fetch(url)
            except Exception:
                raw = None
            if raw and raw.strip() != "null":
                break
            if attempt < 2:
                time.sleep(2 + attempt * 2)
        if not raw or raw.strip() == "null":
            break  # 3次尝试后仍失败：用已拉到的数据
        try:
            arr = json.loads(raw)
        except json.JSONDecodeError:
            break
        if not arr:
            break
        out.extend(arr)
        pn += 1
        time.sleep(0.2)  # 新浪分页限速（实测 0.2s 稳定）
    return out


def is_main_board(code):
    return code.startswith(MAIN_BOARD_PREFIX)


def basic_filter(stocks):
    """
    基础过滤：
      ① 主板白名单（硬条件）
      ② 剔除 ST/*ST/退市整理
      ③ 股价 ≥5 元
      ④ 成交额 ≥2亿（当日近似 20 日均额）
      ⑤ 当天跌停剔除
    返回 (kept, dropped_stats)
    """
    kept, dropped = [], {"perm": 0, "st": 0, "price": 0, "amount": 0, "limitdown": 0, "other": 0}
    for s in stocks:
        code = str(s.get("code", ""))
        if not is_main_board(code):
            dropped["perm"] += 1
            continue
        name = str(s.get("name", ""))
        if "ST" in name.upper() or "退" in name:
            dropped["st"] += 1
            continue
        try:
            price = float(s.get("trade", 0) or 0)
            amount = float(s.get("amount", 0) or 0)
            chg = float(s.get("changepercent", 0) or 0)
            turnover = float(s.get("turnoverratio", 0) or 0)
            mktcap = float(s.get("mktcap", 0) or 0)
        except (TypeError, ValueError):
            dropped["other"] += 1
            continue
        if price < MIN_PRICE:
            dropped["price"] += 1
            continue
        if amount < MIN_AMOUNT:
            dropped["amount"] += 1
            continue
        if chg <= LIMIT_DOWN:
            dropped["limitdown"] += 1
            continue
        kept.append({
            "code": code, "name": name, "price": price, "amount": amount,
            "chg": chg, "turnover": turnover, "mktcap": mktcap,
        })
    return kept, dropped


if __name__ == "__main__":
    import sys
    t0 = time.time()
    all_stocks = fetch_all_stocks()
    print(f"[stock_scanner] 全市场 {len(all_stocks)}只 耗时{time.time()-t0:.0f}s")
    kept, dropped = basic_filter(all_stocks)
    print(f"[stock_scanner] 主板过滤后 {len(kept)}只")
    print(f"[stock_scanner] 剔除明细: {json.dumps(dropped, ensure_ascii=False)}")
    # 展示 top10（按成交额）
    kept_sorted = sorted(kept, key=lambda x: -x["amount"])[:10]
    for k in kept_sorted:
        print(f"  {k['code']} {k['name']} 价{k['price']} 额{k['amount']/1e8:.1f}亿 换手{k['turnover']}%")
