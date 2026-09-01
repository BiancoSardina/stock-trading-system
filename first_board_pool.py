#!/usr/bin/env python3
"""
首板启动池扫描器（2026-08-18 新增，用户指定选股逻辑）
==========================================================
目标：抓"首板启动 → 次日接力 → 第三天溢价"的强势启动股（登海种业型）。

设计原则（与五因子池的区别）：
  · 不设"20日涨幅>30%"硬排除 —— 首板启动股横盘后启动，20日涨幅天然偏高
  · 不设"偏离MA20>12%"硬排除 —— 首板/二板涨停日偏离度瞬时放大是正常形态
  · 行业只做加分项，不做硬过滤 —— 行业映射缺失时最多少加分，不误杀
  · 风险控制靠"次日高开分档 + -5%硬止损 + 第三天必卖"的交易纪律

识别条件（全部满足才入池）：
  ① 沪深主板（600/601/603/605/000/001/002/003）
  ② 非ST/退市，上市>60天（K线>=60根）
  ③ 当日涨停（涨幅≥9.5%，主板10%）
  ④ 昨日非涨停（真首板，非连板）
  ⑤ 收盘=涨停价（封死板，非炸板）
  ⑥ 非一字板（开盘价<涨停价，买得进）
  ⑦ 收盘创20日新高（突破平台）
  ⑧ 放量：成交额≥3亿 且 量比≥1.5（增量资金进场）

启动日识别通道（第二通道，2026-08-21 新增，专治"登海型"盲区）：
  登海 8-17 是 +5.3% 放量大阳（非涨停）→ 首板池第一通道看不见。
  启动日条件：涨幅+4%~9.5%（放量大阳未涨停）且 量比≥2 且 成交额≥3亿
  且 收盘创20日新高或距新高<3% 且 收盘>MA20 且 昨日涨幅<7%（非连板高位）。
  输出"启动日候选"+次日预案（回踩低吸/突破追涨/跌破止损），
  让用户在首板前一天就拿到次日条件单。

质量评分（0-100）：量能30 + 换手20 + 突破20 + 市值10 + 行业10 + 大盘5 + 封板5
  ≥75 S级（重点接力） / 60-74 A级（观察） / <60 放弃

输出：first_board_pool.json + 次日接力预案（高开分档）
"""
import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import stock_scanner
import industry_rank
import short_term

OUTPUT = os.path.join(SCRIPT_DIR, "first_board_pool.json")

# 识别阈值
LIMIT_CHG = 9.5        # 主板涨停判定（10%涨停，容差）
MIN_AMOUNT = 3e8       # 成交额≥3亿（放量启动）
MIN_VOL_RATIO = 1.5    # 量比≥1.5
NEW_HIGH_N = 20        # 20日新高
MIN_KLINE = 60         # 上市>60天（K线根数）

# 启动日识别阈值（第二通道，2026-08-21）
LAUNCH_MIN_CHG = 4.0   # 涨幅≥4%（放量大阳）
LAUNCH_MAX_CHG = 9.5   # 未涨停
LAUNCH_VOL_RATIO = 2.0 # 量比≥2（显著放量）
LAUNCH_PREV_MAX = 7.0  # 昨日涨幅<7%（排除连板高位）
LAUNCH_NEWHIGH_GAP = 3.0  # 距20日新高<3%

# 评分权重
W_AMOUNT, W_TURNOVER, W_BREAK, W_MCAP, W_IND, W_MKT, W_SEAL = 30, 20, 20, 10, 10, 5, 5
S_GRADE, A_GRADE = 75, 60


def limit_price(prev_close):
    """主板涨停价（四舍五入到分）"""
    return round(prev_close * 1.1, 2)


def score_candidate(code, name, s, kl, ind_score, mkt_score):
    """首板质量评分 + 次日预案"""
    closes = [float(k["close"]) for k in kl]
    highs = [float(k["high"]) for k in kl]
    vols = [float(k["volume"]) for k in kl]
    prev_close = float(kl[-2]["close"]) if len(kl) >= 2 else 0
    cur = float(kl[-1]["close"])
    zt = limit_price(prev_close)

    # --- 量能30 ---
    amount = float(s.get("amount", 0) or 0)
    if 3e8 <= amount <= 10e8:
        f_amount = 30
    elif 10e8 < amount <= 20e8:
        f_amount = 20
    else:
        f_amount = 15

    # --- 换手20 ---
    turnover = float(s.get("turnoverratio", 0) or 0)
    if 5 <= turnover <= 12:
        f_turnover = 20
    elif 3 <= turnover < 5 or 12 < turnover <= 20:
        f_turnover = 12
    else:
        f_turnover = 5

    # --- 突破20：创20日新高 + 距离前高幅度 ---
    prev_highs = highs[-NEW_HIGH_N - 1:-1] if len(highs) > NEW_HIGH_N else highs[:-1]
    prev_high = max(prev_highs) if prev_highs else cur
    if cur > prev_high:
        gap = (cur / prev_high - 1) * 100
        f_break = 20 if gap >= 3 else (15 if gap >= 1 else 12)
    else:
        f_break = 5

    # --- 市值10 ---
    mktcap = float(s.get("mktcap", 0) or 0)
    if 50e8 <= mktcap <= 300e8:
        f_mcap = 10
    elif 30e8 <= mktcap < 50e8 or 300e8 < mktcap <= 500e8:
        f_mcap = 6
    else:
        f_mcap = 3

    # --- 行业10（只加分，不硬过滤）---
    if ind_score >= 70:
        f_ind = 10
    elif ind_score >= 55:
        f_ind = 6
    elif ind_score >= 40:
        f_ind = 3
    else:
        f_ind = 0

    # --- 大盘5 ---
    f_mkt = 5 if mkt_score >= 70 else (3 if mkt_score >= 60 else 0)

    # --- 封板5（识别条件已要求收盘=涨停价，此处恒5，保留字段）---
    f_seal = 5

    total = f_amount + f_turnover + f_break + f_mcap + f_ind + f_mkt + f_seal
    grade = "S" if total >= S_GRADE else ("A" if total >= A_GRADE else "C")

    # --- 次日接力预案（高开分档）---
    plan = build_next_plan(prev_close, zt)

    return {
        "code": code, "name": name,
        "score": total, "grade": grade,
        "close": cur, "chg": round((cur / prev_close - 1) * 100, 2),
        "zt_price": zt, "amount": round(amount / 1e8, 2),
        "turnover": turnover, "mktcap": round(mktcap / 1e8, 1),
        "vol_ratio": round(float(vols[-1]) / (sum(vols[-6:-1]) / 5), 2) if len(vols) >= 6 and sum(vols[-6:-1]) else None,
        "break_high": round(prev_high, 2),
        "industry": s.get("industry", ""), "industry_score": ind_score,
        "days": 1, "plan": plan,
    }


def build_next_plan(prev_close, zt_price):
    """次日接力预案（禁止追高，-5%硬止损，第三天必卖）"""
    return {
        "高开0-3%": f"最优买点：开盘回踩昨收{prev_close:.2f}附近低吸（登海型买点）",
        "高开3-6%": f"半路跟：回踩不破开盘价可买，破开盘价放弃",
        "高开>6%": "不追！等炸板回踩或放弃（高开追=接盘）",
        "平开/低开<3%": f"观察10分钟，放量翻红再跟；不翻红放弃",
        "低开>3%": "直接放弃（昨日资金出货，弱转弱）",
        "止损": f"买入价-5% 或 跌破昨收{prev_close:.2f}，无条件走",
        "卖出": "第三天必卖：高开冲高不封板/炸板卖；平开破均线卖；低开直接走。目标+3~8%",
    }


def build_launch_plan(close, day_high, ma5):
    """启动日次日预案（放量大阳后：回踩低吸/突破追涨/跌破止损）"""
    dip = round(max(ma5, close * 0.97), 2)          # 回踩位：MA5 与收盘-3%取较高（动态支撑）
    chase = round(day_high * 1.01, 2)                # 追涨位：突破当日高点+1%
    stop = round(day_high * 0.95, 2)                 # 止损：当日高点-5%（防假突破深回踩）
    return {
        "回踩买入": f"回踩 {dip} 附近企稳（缩量止跌）→ 买入（登海8-17后8-18平开低吸型）",
        "突破追涨": f"放量站稳 {chase}（突破当日高点{day_high:.2f}+1%）→ 买入",
        "止损": f"跌破 {stop}（当日高点-5%）→ 无条件走；破MA5缩量洗盘可等MA10，破MA10必走",
        "卖出": "次日冲涨停封死→持有吃第三天溢价；冲高回落破分时均线→卖；3天内必了结",
        "不追条件": f"次日高开>5%不追（{day_high*1.05:.2f}以上），等回踩或放弃",
    }


def score_launch_candidate(code, name, s, kl, ind_score, mkt_score):
    """启动日质量评分（放量大阳未涨停，次日接力）"""
    closes = [float(k["close"]) for k in kl]
    highs = [float(k["high"]) for k in kl]
    vols = [float(k["volume"]) for k in kl]
    cur = closes[-1]
    prev_close = closes[-2]

    # --- 量能30（同首板口径）---
    amount = float(s.get("amount", 0) or 0)
    if 3e8 <= amount <= 10e8:
        f_amount = 30
    elif 10e8 < amount <= 20e8:
        f_amount = 20
    else:
        f_amount = 15

    # --- 换手20 ---
    turnover = float(s.get("turnoverratio", 0) or 0)
    if 5 <= turnover <= 12:
        f_turnover = 20
    elif 3 <= turnover < 5 or 12 < turnover <= 20:
        f_turnover = 12
    else:
        f_turnover = 5

    # --- 突破20：当日高点创20日新高或距新高<3% ---
    prev_highs = highs[-NEW_HIGH_N - 1:-1] if len(highs) > NEW_HIGH_N else highs[:-1]
    prev_high = max(prev_highs) if prev_highs else cur
    day_high = highs[-1]
    gap_to_high = (day_high / prev_high - 1) * 100 if prev_high else 0
    if day_high > prev_high:
        f_break = 20 if gap_to_high >= 3 else (15 if gap_to_high >= 1 else 12)
    elif prev_high - day_high <= prev_high * LAUNCH_NEWHIGH_GAP / 100:
        f_break = 12  # 距新高<3%，次日突破即新高
    else:
        f_break = 5

    # --- 市值10 ---
    mktcap = float(s.get("mktcap", 0) or 0)
    if 50e8 <= mktcap <= 300e8:
        f_mcap = 10
    elif 30e8 <= mktcap < 50e8 or 300e8 < mktcap <= 500e8:
        f_mcap = 6
    else:
        f_mcap = 3

    # --- 行业10（只加分）---
    if ind_score >= 70:
        f_ind = 10
    elif ind_score >= 55:
        f_ind = 6
    elif ind_score >= 40:
        f_ind = 3
    else:
        f_ind = 0

    # --- 大盘5 ---
    f_mkt = 5 if mkt_score >= 70 else (3 if mkt_score >= 60 else 0)

    # --- 大阳质量5：实体涨幅（收盘-开盘）/昨收 ---
    open_px = float(kl[-1]["open"])
    body = (cur - open_px) / prev_close * 100
    f_body = 5 if body >= 4 else (3 if body >= 2 else 0)

    total = f_amount + f_turnover + f_break + f_mcap + f_ind + f_mkt + f_body
    grade = "S" if total >= S_GRADE else ("A" if total >= A_GRADE else "C")

    ma5 = sum(closes[-5:]) / 5
    vol_ratio = vols[-1] / (sum(vols[-6:-1]) / 5) if sum(vols[-6:-1]) else 0
    plan = build_launch_plan(cur, highs[-1], ma5)

    return {
        "code": code, "name": name, "score": total, "grade": grade,
        "close": cur, "chg": round((cur / prev_close - 1) * 100, 2),
        "day_high": round(highs[-1], 2), "amount": round(amount / 1e8, 2),
        "turnover": turnover, "mktcap": round(mktcap / 1e8, 1),
        "vol_ratio": round(vol_ratio, 2), "break_high": round(prev_high, 2),
        "industry": s.get("industry", ""), "industry_score": ind_score,
        "days": 0, "plan": plan, "type": "launch",
    }


def scan():
    t0 = time.time()
    # 1) 市场评分（大盘环境闸门：<60 仅记录不推荐）
    mkt_score, mkt_status = 0, "?"
    try:
        indices = short_term.get_indices()
        mkt_lines = short_term.market_score(indices)
        for ln in mkt_lines:
            if "市场评分" in ln and "分" in ln:
                try:
                    mkt_score = int("".join(c for c in ln.split("分")[0] if c.isdigit())[-2:])
                except Exception:
                    pass
        mkt_status = "A" if mkt_score >= 80 else ("B" if mkt_score >= 65 else ("C" if mkt_score >= 50 else "D"))
    except Exception as e:
        print(f"[first_board] 市场评分失败: {e}", file=sys.stderr)
    print(f"[first_board] 市场评分: {mkt_status}级 {mkt_score}分", file=sys.stderr)

    # 2) 全市场 + 基础过滤（首板池放款版：成交额≥3亿）
    all_stocks = stock_scanner.fetch_all_stocks()
    print(f"[first_board] 全市场 {len(all_stocks)}只", file=sys.stderr)
    if len(all_stocks) < 3000:
        # 数据拉取失败判定：全市场数量异常 → 明确失败（晚间任务据此重试）
        _save({"date": time.strftime("%Y-%m-%d"), "market_status": mkt_status,
               "market_score": mkt_score, "candidates": [], "failed_kline": [],
               "error": f"全市场仅{len(all_stocks)}只(<3000)，数据拉取失败", "all_count": len(all_stocks)})
        print(f"[first_board] ❌ 全市场数据异常({len(all_stocks)}只)，本次扫描失败", file=sys.stderr)
        return {"ok": False, "all_count": len(all_stocks), "error": "全市场数据异常"}
    kept = []
    for s in all_stocks:
        code = str(s.get("code", ""))
        if not stock_scanner.is_main_board(code):
            continue
        name = str(s.get("name", ""))
        if "ST" in name.upper() or "退" in name:
            continue
        try:
            chg = float(s.get("changepercent", 0) or 0)
            amount = float(s.get("amount", 0) or 0)
            price = float(s.get("trade", 0) or 0)
        except (TypeError, ValueError):
            continue
        if price < 5 or amount < MIN_AMOUNT or chg < LIMIT_CHG:
            continue
        s["code"], s["name"] = code, name
        kept.append(s)
    print(f"[first_board] 当日涨停候选 {len(kept)}只", file=sys.stderr)
    if not kept:
        print("[first_board] 今日无首板候选", file=sys.stderr)
        _save({"date": time.strftime("%Y-%m-%d"), "market_status": mkt_status,
               "market_score": mkt_score, "candidates": [], "failed_kline": [],
               "all_count": len(all_stocks)})
        return {"ok": True, "all_count": len(all_stocks), "candidates": 0}

    # 3) 行业映射（容错版）
    imap = industry_rank.load_industry_map()
    ind_index = industry_rank.stock_industry_index(imap)
    ind_scores = {}
    for name, info in imap.get("industries", {}).items():
        ind_scores[name] = 0  # 行业分由 score_industries 算，此处用当日简化：直接用行业成分涨跌？简化：跳过
    # 简化行业分：行业成分当日平均涨幅
    try:
        ind_scores = {}
        idx = {str(x.get("code", "")): x for x in all_stocks}
        for name, info in imap.get("industries", {}).items():
            members = [idx[c] for c in info.get("stocks", []) if c in idx]
            if members:
                ind_scores[name] = round(sum(float(m.get("changepercent", 0) or 0) for m in members) / len(members) * 10, 0)
    except Exception:
        pass

    # 4) 逐只拉K线识别真首板（只处理涨停候选，量小）
    candidates, failed_kline = [], []
    for i, s in enumerate(kept):
        code, name = s["code"], s["name"]
        try:
            kl = short_term.get_kline(code, 60)
        except Exception:
            kl = []
        if len(kl) < MIN_KLINE:
            failed_kline.append(f"{code} {name} K线不足")
            continue
        closes = [float(k["close"]) for k in kl]
        prev_close = closes[-2]
        prev_chg = (prev_close / closes[-3] - 1) * 100 if len(closes) >= 3 else 0
        cur = closes[-1]
        zt = limit_price(prev_close)
        open_px = float(kl[-1]["open"])
        # 真首板：昨日非涨停
        if prev_chg >= LIMIT_CHG:
            continue
        # 封死板：收盘≥涨停价-0.5%
        if cur < zt * 0.995:
            continue
        # 非一字：开盘价 < 涨停价
        if open_px >= zt * 0.995:
            continue
        # 20日新高
        prev_highs = [float(k["high"]) for k in kl[:-1]]
        if cur <= max(prev_highs[-NEW_HIGH_N:]):
            continue
        # 量比
        vols = [float(k["volume"]) for k in kl]
        vol_ratio = vols[-1] / (sum(vols[-6:-1]) / 5) if sum(vols[-6:-1]) else 0
        if vol_ratio < MIN_VOL_RATIO:
            continue
        # 行业
        ind_name = ind_index.get(code, "")
        ind_score = ind_scores.get(ind_name, 0) if ind_name else 0
        s["industry"] = ind_name
        try:
            c = score_candidate(code, name, s, kl, ind_score, mkt_score)
        except Exception as e:
            failed_kline.append(f"{code} {name} 评分异常: {e}")
            continue
        if c["score"] >= A_GRADE:
            candidates.append(c)
        time.sleep(0.15)

    # 5) 启动日识别通道（放量大阳未涨停，登海型盲区 2026-08-21）
    #    粗筛：涨幅+4%~9.5% + 成交额≥3亿 + 非ST主板，再逐只K线验证
    launch_candidates = []
    if mkt_score >= 50:  # 市场环境闸门：C级以下不推启动日（弱市放量大阳多为出货）
        launch_kept = []
        for s in all_stocks:
            code = str(s.get("code", ""))
            if not stock_scanner.is_main_board(code):
                continue
            name = str(s.get("name", ""))
            if "ST" in name.upper() or "退" in name:
                continue
            try:
                chg = float(s.get("changepercent", 0) or 0)
                amount = float(s.get("amount", 0) or 0)
                price = float(s.get("trade", 0) or 0)
            except (TypeError, ValueError):
                continue
            if price < 5 or amount < MIN_AMOUNT:
                continue
            if LAUNCH_MIN_CHG <= chg < LAUNCH_MAX_CHG:
                s["code"], s["name"] = code, name
                launch_kept.append(s)
        print(f"[first_board] 启动日粗筛 {len(launch_kept)}只", file=sys.stderr)
        for s in launch_kept:
            code, name = s["code"], s["name"]
            try:
                kl = short_term.get_kline(code, 60)
            except Exception:
                kl = []
            if len(kl) < MIN_KLINE:
                continue
            closes = [float(k["close"]) for k in kl]
            highs = [float(k["high"]) for k in kl]
            vols = [float(k["volume"]) for k in kl]
            prev_close = closes[-2]
            prev_chg = (prev_close / closes[-3] - 1) * 100 if len(closes) >= 3 else 0
            cur = closes[-1]
            open_px = float(kl[-1]["open"])
            # 昨日非大涨（排除连板高位）
            if prev_chg >= LAUNCH_PREV_MAX:
                continue
            # 量比≥2（显著放量）
            vol_ratio = vols[-1] / (sum(vols[-6:-1]) / 5) if sum(vols[-6:-1]) else 0
            if vol_ratio < LAUNCH_VOL_RATIO:
                continue
            # 收盘>MA20（趋势成立）
            ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else closes[-1]
            if cur <= ma20:
                continue
            # 创20日新高或距新高<3%（用当日高点判定：盘中已摸前高=攻击性成立）
            prev_highs = [float(k["high"]) for k in kl[:-1]]
            prev_high = max(prev_highs[-NEW_HIGH_N:]) if len(prev_highs) >= NEW_HIGH_N else (prev_highs[-1] if prev_highs else cur)
            day_high = highs[-1]
            if day_high <= prev_high and (prev_high - day_high) / prev_high * 100 > LAUNCH_NEWHIGH_GAP:
                continue
            # 收阳（当日大阳实体）
            if cur <= open_px:
                continue
            # 行业
            ind_name = ind_index.get(code, "")
            ind_score = ind_scores.get(ind_name, 0) if ind_name else 0
            s["industry"] = ind_name
            try:
                c = score_launch_candidate(code, name, s, kl, ind_score, mkt_score)
            except Exception as e:
                failed_kline.append(f"{code} {name} 启动日评分异常: {e}")
                continue
            if c["score"] >= A_GRADE:
                launch_candidates.append(c)
            time.sleep(0.15)
        launch_candidates.sort(key=lambda x: -x["score"])

    candidates.sort(key=lambda x: -x["score"])
    _save({"date": time.strftime("%Y-%m-%d"), "market_status": mkt_status,
           "market_score": mkt_score, "candidates": candidates,
           "launch_candidates": launch_candidates,
           "failed_kline": failed_kline, "all_count": len(all_stocks)})
    print(f"[first_board] 首板池 {len(candidates)}只 (S:{sum(1 for c in candidates if c['grade']=='S')} A:{sum(1 for c in candidates if c['grade']=='A')}) "
          f"启动日池 {len(launch_candidates)}只 (S:{sum(1 for c in launch_candidates if c['grade']=='S')} A:{sum(1 for c in launch_candidates if c['grade']=='A')}) "
          f"耗时{time.time()-t0:.0f}s", file=sys.stderr)
    for c in candidates:
        print(f"  {c['grade']} {c['code']} {c['name']:<6} {c['score']}分 涨幅{c['chg']:+.1f}% 收{c['close']} "
              f"额{c['amount']}亿 换手{c['turnover']}% 量比{c['vol_ratio']} 行业{c['industry']}({c['industry_score']})", file=sys.stderr)
    for c in launch_candidates:
        print(f"  [启动日] {c['grade']} {c['code']} {c['name']:<6} {c['score']}分 涨幅{c['chg']:+.1f}% 收{c['close']} "
              f"量比{c['vol_ratio']} 行业{c['industry']}({c['industry_score']})", file=sys.stderr)
    return {"ok": True, "all_count": len(all_stocks), "candidates": len(candidates),
            "launch_candidates": len(launch_candidates)}


def _save(data):
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    scan()
