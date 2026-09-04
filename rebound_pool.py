#!/usr/bin/env python3
"""
超跌反弹候选池 ReboundPool V1.0（2026-08-12 与用户确认逻辑后实现）
================================================================
定位：手动运行的辅助选股工具（无定时任务、无生命周期、无自动信号）。
  场景：趋势池（stock_pool.json core）空池/无票可看时，手动运行本脚本，
        从全市场筛出「前期强势 + 充分回撤 + 跌速衰竭 + 止跌确认」的候选，
        供盘中监控。只输出监控清单与触发条件，不产生买入信号。

与趋势池的关系（完全独立）：
  TrendPool（stock_pool.py，17:30 定时）= 强者恒强，趋势通道
  ReboundPool（本脚本，手动）= 超跌+止跌，反弹通道
  趋势池空 → 手动跑本脚本补候选；同一时刻只开一个通道。

核心设计（用户 V1.0 方案 + 2026-08-12 确认的 4 点修改）：
  ① 左侧条件：前期强势（30日窗口，压缩自原60日）+ 20日高点回撤≥10% + RSI<35
  ② 右侧确认：止跌信号低延迟化（不创新低优先，不等 MA5 修复完）
  ③ 技术评分 + 行业热度双门槛：个股必须超跌后止跌，所属行业也必须有资金活跃。
     行业不热不以个股反弹猜资金回流，只保留在后台排除统计。
     （用户第五层给了 RSI 分但第九层总分模型漏列，并入超跌维度补齐，不破100分制）
  ④ A级硬门槛：今日不创新低 + 至少1项其他止跌信号（score≥75 也不破例）
  ⑤ 硬排除：连续3日创新低 / 近3日≥2次跌停 / 回撤<10% / 前期不强势 / RSI≥35 / 数据不足

运行：
  python3 rebound_pool.py              # 全量生成（~10分钟），写 rebound_pool.json
  python3 rebound_pool.py --top 20     # 只显示前20只
  python3 rebound_pool.py --fast       # 降级：按成交额前400只精筛（~4分钟）
  python3 rebound_pool.py --limit 100  # 测试：只精筛前100只（不落盘）
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import short_term   # 复用 get_rt / calc_rsi / calc_atr / market_score
import stock_scanner
import industry_rank

POOL_PATH = os.path.join(SCRIPT_DIR, "rebound_pool.json")
TREND_POOL_PATH = os.path.join(SCRIPT_DIR, "stock_pool.json")

KLINE_SLEEP = 0.05        # K线请求间隔（防456限流，同 stock_pool）
FAST_PRESCREEN = 400      # --fast 按成交额粗筛前N只
MIN_KLEN = 35             # K线最少根数（算30日强势+20日回撤）

# ===== 超跌维度（25分 = 回撤20 + RSI 5）=====
DD_MIN = -10.0            # 回撤硬门槛：当前价/20日最高价-1 <= -10%
RSI_MAX = 35.0            # RSI 硬门槛：<35 才进入超跌状态

# ===== 前期强势维度（20分，30日窗口压缩自用户原60日）=====
PRIOR_RISE30_MIN = 25.0   # 30日最高/30日前收盘 -1 >= 25%
PRIOR_RISE20_MIN = 15.0   # 20日最高/20日前收盘 -1 >= 15%
# 满足任一即通过；评分取两窗口高分，不叠加

# ===== 硬排除 =====
LIMIT_DOWN_CHG = -9.8     # 跌停判定（主板非ST）
LIMIT_DOWN_MAX_3D = 1     # 近3日跌停≥2次 → 排除（风险事件释放中）

# ===== 行业热度（底部企稳的资金确认） =====
# 行业评分沿用主股票池：周期涨幅、成交额、趋势及上涨家数。底部股本身偏弱，
# 因而要求行业至少中强，且当日广度/短期动量没有转弱，避免只看个股形态猜反转。
INDUSTRY_MIN_SCORE = 60
INDUSTRY_MIN_UP_RATIO = 0.50


def _get_kline(code, days=120):
    """个股日K线（quotes.sina.cn 抗限流端点，同 stock_pool.py）"""
    pref = short_term.get_prefix(code)
    url = (f"https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
           f"?symbol={pref}{code}&scale=240&ma=no&datalen={days}")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore"))
    except Exception:
        return []


def _kline_with_today(code):
    """K线 + 盘中合并实时行情（新浪日K盘中不含当日；收盘后若已更新则不合并）"""
    rt = short_term.get_rt(code)
    if not rt or rt["cur"] <= 0:
        return None
    kline = _get_kline(code, 120)
    if not kline:
        return None
    last_day = kline[-1].get("day", "")
    today = datetime.now().strftime("%Y-%m-%d")
    if last_day != today:
        kline = kline + [{
            "day": today, "open": str(rt["open"]), "high": str(rt["high"]),
            "low": str(rt["low"]), "close": str(rt["cur"]), "volume": str(rt["vol"]),
        }]
    return kline


def _chg_series(closes):
    """收盘价序列 → 每日涨跌幅序列（首日None）"""
    out = [None]
    for i in range(1, len(closes)):
        out.append((closes[i] / closes[i - 1] - 1) * 100)
    return out


def oversold_score(dd, rsi):
    """超跌程度 25分 = 回撤20 + RSI5（非单调：极端超跌反而不加分）"""
    if dd <= -35:
        s_dd = 4
    elif dd <= -30:
        s_dd = 12
    elif dd <= -20:
        s_dd = 16
    elif dd <= -15:
        s_dd = 12
    else:
        s_dd = 6
    if rsi <= 25:
        s_rsi = 3
    elif rsi <= 30:
        s_rsi = 5
    else:
        s_rsi = 3
    return min(25, s_dd + s_rsi), s_dd, s_rsi


def prior_score(rise30, rise20):
    """前期强势 20分：两窗口分别计分取高者，不叠加"""
    s30 = 12 if rise30 > 40 else 8 if rise30 >= 25 else 0
    s20 = 12 if rise20 > 25 else 8 if rise20 >= 15 else 0
    return max(s30, s20)


def decay_score(closes, opens, highs, lows, chg):
    """跌速衰竭 20分：卖压减弱信号，非精确预测"""
    s = 0
    signals = []
    # 今日不创新低 +8
    if lows[-1] >= lows[-2]:
        s += 8
        signals.append("不创新低")
    # 近3日跌幅明显收窄（vs 前3日，收窄30%+或转正）+6
    if chg[-6] is not None and chg[-3] is not None:
        chg3_prev = sum(x for x in chg[-6:-3] if x is not None)
        chg3 = sum(x for x in chg[-3:] if x is not None)
        if chg3_prev < 0 and chg3 > chg3_prev * 0.7:
            s += 6
            signals.append("近3日跌幅收窄")
    # 今日跌幅收窄（今日 >= 昨日）+3
    if chg[-1] is not None and chg[-2] is not None and chg[-1] >= chg[-2]:
        s += 3
        signals.append("今日跌幅收窄")
    # 下影线明显（下影 > 实体区间30%）+3
    rng = highs[-1] - lows[-1]
    if rng > 0:
        lower_shadow = min(opens[-1], closes[-1]) - lows[-1]
        if lower_shadow / rng > 0.3:
            s += 3
            signals.append("下影线")
    return min(20, s), signals


def stabilize_score(closes, opens, highs, lows, vols, ma5):
    """止跌确认 20分（低延迟版：不创新低优先，不等 MA5 修复）"""
    s = 0
    signals = []
    yang = closes[-1] > opens[-1]
    # 收复MA5 +5
    if closes[-1] > ma5:
        s += 5
        signals.append("收复MA5")
    # 阳线 +3
    if yang:
        s += 3
        signals.append("阳线")
    # 放量上涨 +5（量>5日均量1.2倍 且 阳线）
    avgv = sum(vols[-6:-1]) / 5 if len(vols) >= 6 else 0
    if avgv > 0 and vols[-1] > avgv * 1.2 and yang:
        s += 5
        signals.append("放量")
    # 突破昨日高点 +4
    if highs[-1] > highs[-2]:
        s += 4
        signals.append("破昨日高")
    # 连续两日止跌（low 连续不创新低）+3
    if lows[-1] >= lows[-2] and lows[-2] >= lows[-3]:
        s += 3
        signals.append("连2日止跌")
    # A级硬门槛：今日不创新低 + 至少1项其他信号
    stabilize_ok = (lows[-1] >= lows[-2]) and len(signals) >= 1
    return min(20, s), signals, stabilize_ok


def risk_score(atr_pct, limitdown_3d):
    """风险 15分：波动映射 + 近3日跌停扣分"""
    if atr_pct is None:
        return 0
    if atr_pct < 3:
        s = 15
    elif atr_pct < 5:
        s = 12
    elif atr_pct < 8:
        s = 8
    else:
        s = 4
    if limitdown_3d >= 1:
        s -= 5
    return max(0, s)


def industry_heat(industry):
    """返回 (是否热, 加分, 原因)。行业数据缺失一律不放行。"""
    if not isinstance(industry, dict):
        return False, 0, "行业数据缺失"
    try:
        score = float(industry.get("score"))
        up_ratio = float(industry.get("up_ratio"))
        avg_chg = float(industry.get("avg_chg"))
    except (TypeError, ValueError):
        return False, 0, "行业数据无效"
    period = industry.get("period") or (None, None)
    chg5 = period[0] if len(period) >= 1 else None
    try:
        short_term_up = float(chg5) >= 0 if chg5 is not None else avg_chg > 0
    except (TypeError, ValueError):
        short_term_up = avg_chg > 0
    if score < INDUSTRY_MIN_SCORE:
        return False, 0, f"行业分{score:.0f}<{INDUSTRY_MIN_SCORE}"
    if up_ratio < INDUSTRY_MIN_UP_RATIO:
        return False, 0, f"行业上涨家数{up_ratio:.0%}<{INDUSTRY_MIN_UP_RATIO:.0%}"
    if not short_term_up:
        return False, 0, "行业短期动量未转强"
    bonus = 15 if score >= 80 else 12 if score >= 70 else 8
    return True, bonus, f"行业{score:.0f}分/上涨{up_ratio:.0%}/5日{float(chg5):+.1f}%" if chg5 is not None else \
        f"行业{score:.0f}分/上涨{up_ratio:.0%}/当日{avg_chg:+.1f}%"


def evaluate_stock(code, name, amount, industry_name=None, industry_data=None):
    """单只股票精筛评分，返回条目 dict 或 None（数据失败）或 {"_exclude": 原因}"""
    hot, industry_bonus, industry_reason = industry_heat(industry_data)
    if not hot:
        return {"_exclude": f"行业不热：{industry_reason}"}
    kline = _kline_with_today(code)
    if not kline or len(kline) < MIN_KLEN:
        return None
    try:
        closes = [float(b["close"]) for b in kline]
        opens = [float(b["open"]) for b in kline]
        highs = [float(b["high"]) for b in kline]
        lows = [float(b["low"]) for b in kline]
        vols = [int(b["volume"]) for b in kline]
    except Exception:
        return None

    cur = closes[-1]
    chg = _chg_series(closes)

    # 硬排除1：连续3日创新低（下跌中继，非超跌）
    if lows[-1] < lows[-2] < lows[-3]:
        return {"_exclude": "连续3日创新低"}
    # 硬排除2：近3日≥2次跌停（风险事件释放中）
    limitdown_3d = sum(1 for x in chg[-3:] if x is not None and x <= LIMIT_DOWN_CHG)
    if limitdown_3d >= 2:
        return {"_exclude": "近3日≥2次跌停"}
    # 硬排除3：回撤不足（当前价/20日最高价-1 > -10%）
    dd = (closes[-1] / max(highs[-20:]) - 1) * 100
    if dd > DD_MIN:
        return {"_exclude": "回撤不足"}
    # 硬排除4：前期不强势（30日 或 20日窗口均不达标）
    rise30 = (max(highs[-30:]) / closes[-31] - 1) * 100
    rise20 = (max(highs[-20:]) / closes[-21] - 1) * 100
    if rise30 < PRIOR_RISE30_MIN and rise20 < PRIOR_RISE20_MIN:
        return {"_exclude": "前期不强"}
    # 硬排除5：RSI 未进入超跌状态
    rsi = short_term.calc_rsi(closes)
    if rsi is None or rsi >= RSI_MAX:
        return {"_exclude": "RSI过高"}

    ma5 = sum(closes[-5:]) / 5
    atr14 = short_term.calc_atr(highs, lows, closes, 14)
    atr_pct = round(atr14 / cur * 100, 2) if atr14 else None

    s_ov, s_dd, s_rsi = oversold_score(dd, rsi)
    s_prior = prior_score(rise30, rise20)
    s_decay, decay_sig = decay_score(closes, opens, highs, lows, chg)
    s_stab, stab_sig, stabilize_ok = stabilize_score(closes, opens, highs, lows, vols, ma5)
    s_risk = risk_score(atr_pct, limitdown_3d)

    tech_score = min(100, s_ov + s_prior + s_decay + s_stab + s_risk)
    total = min(100, tech_score + industry_bonus)

    # 热行业只能确认资金环境，不能挽救技术形态差的个股。
    if total >= 75 and tech_score >= 65 and stabilize_ok:
        level = "A"
    elif total >= 60 and tech_score >= 55:
        level = "B"
    elif total >= 50 and tech_score >= 50:
        level = "C"
    else:
        return {"_exclude": "评分不足"}

    # 监控点位（前瞻+预案）：突破→关注 / 站稳→确认 / 跌破→放弃
    watch_price = round(highs[-2], 2)
    ma5_r = round(ma5, 2)
    stop_low = round(min(lows[-1], lows[-2]), 2)

    return {
        "code": code, "name": name, "price": round(cur, 2),
        "score": total, "tech_score": tech_score, "level": level,
        "industry": {"name": industry_name, "score": industry_data["score"],
                     "up_ratio": industry_data["up_ratio"], "period": industry_data.get("period"),
                     "heat_reason": industry_reason, "bonus": industry_bonus},
        "factor": {"oversold": s_ov, "prior": s_prior, "decay": s_decay,
                   "stabilize": s_stab, "risk": s_risk, "industry": industry_bonus},
        "detail": {
            "dd20": round(dd, 2), "rise30": round(rise30, 2), "rise20": round(rise20, 2),
            "rsi": round(rsi, 1), "atr_pct": atr_pct,
            "decay_signals": decay_sig, "stabilize_signals": stab_sig,
        },
        "stabilize_ok": stabilize_ok,
        "trigger": {"watch": watch_price, "ma5": ma5_r, "stop_low": stop_low},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25, help="只显示前N只（默认25）")
    ap.add_argument("--limit", type=int, default=0, help="只精筛前N只（测试用，不落盘）")
    ap.add_argument("--fast", action="store_true", help="降级：按成交额前400只精筛")
    args = ap.parse_args()

    t0 = time.time()
    today = datetime.now().strftime("%Y-%m-%d")

    # ① 市场状态（仅参考提示，不强制开关——用户手动决定使用时机）
    try:
        short_term.market_score()
        m = short_term.MARKET or {}
        market_score_val = m.get("score", 60)
        market_status = m.get("state", "B")
    except Exception:
        market_score_val, market_status = 60, "B"

    # ② 趋势池 core 数量（联动提示：空池才需要本池补候选）
    trend_core = 0
    try:
        with open(TREND_POOL_PATH, encoding="utf-8") as f:
            trend_core = len(json.load(f).get("core_pool", []))
    except Exception:
        pass

    # ③ 全市场 + 基础过滤（复用 stock_scanner：主板/ST/价格/成交额2亿/跌停）
    all_stocks = stock_scanner.fetch_all_stocks()
    kept, dropped = stock_scanner.basic_filter(all_stocks)
    print(f"[rebound_pool] 全市场{len(all_stocks)} → 基础过滤后{len(kept)}只 "
          f"(剔除:{json.dumps(dropped, ensure_ascii=False)})", file=sys.stderr, flush=True)
    if not kept:
        print("[rebound_pool] ❌ 基础过滤后为空，中止")
        sys.exit(1)

    # ④ 行业热度。行业映射或评分不完整时不产生新池，保留旧结果，避免把
    # “行业未知”误当成“行业不热”或绕过资金确认。
    industry_map = industry_rank.build_industry_map(force=False)
    industry_scores = industry_rank.score_industries(all_stocks, industry_map)
    industry_index = industry_rank.stock_industry_index(industry_map)
    if len(industry_scores) < 40:
        print(f"[rebound_pool] ❌ 行业热度数据不完整({len(industry_scores)}个行业)，保留旧池", file=sys.stderr)
        sys.exit(2)
    print(f"[rebound_pool] 行业热度可用 {len(industry_scores)}个行业；"
          f"门槛≥{INDUSTRY_MIN_SCORE}分、上涨家数≥{INDUSTRY_MIN_UP_RATIO:.0%}、短期动量不弱",
          file=sys.stderr, flush=True)

    # ⑤ 粗筛（实时数据）：当日接近涨停的不是超跌候选；按成交额排序
    candidates = [k for k in kept if k["chg"] < 9.5]
    candidates.sort(key=lambda x: -x["amount"])
    if args.fast:
        candidates = candidates[:FAST_PRESCREEN]
        print(f"[rebound_pool] --fast 降级：按成交额粗筛前{len(candidates)}只", file=sys.stderr, flush=True)
    elif args.limit:
        candidates = candidates[:args.limit]
    print(f"[rebound_pool] 精筛目标 {len(candidates)}只", file=sys.stderr, flush=True)

    # ⑥ 逐只精筛评分
    results, hard_excl = [], Counter()
    for i, c in enumerate(candidates):
        industry_name = industry_index.get(c["code"])
        e = evaluate_stock(c["code"], c["name"], c["amount"], industry_name,
                           industry_scores.get(industry_name))
        if e:
            if e.get("_exclude"):
                hard_excl[e["_exclude"]] += 1
            else:
                results.append(e)
        else:
            hard_excl["数据不足/K线失败"] += 1
        time.sleep(KLINE_SLEEP)
        if (i + 1) % 100 == 0:
            print(f"[rebound_pool] 精筛进度 {i+1}/{len(candidates)}", file=sys.stderr, flush=True)

    # ⑦ 分级排序
    results.sort(key=lambda x: (-x["score"], -x["tech_score"], x["code"]))
    a_lv = [e for e in results if e["level"] == "A"]
    b_lv = [e for e in results if e["level"] == "B"]
    c_lv = [e for e in results if e["level"] == "C"]
    print(f"[rebound_pool] 达标 {len(results)}只 (A{a_lv.__len__()}/B{len(b_lv)}/C{len(c_lv)}) 耗时{time.time()-t0:.0f}s",
          file=sys.stderr, flush=True)

    # ⑧ 落盘（--limit 测试模式不写生产文件）
    if not args.limit:
        out = {
            "date": today,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "market_status": market_status, "market_score": market_score_val,
            "trend_pool_core": trend_core,
            "industry_gate": {"min_score": INDUSTRY_MIN_SCORE, "min_up_ratio": INDUSTRY_MIN_UP_RATIO,
                              "score_count": len(industry_scores)},
            "pool": a_lv + b_lv + c_lv,
        }
        tmp = POOL_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, POOL_PATH)
        print(f"[rebound_pool] ✅ rebound_pool.json 已写入 耗时{time.time()-t0:.0f}s", file=sys.stderr, flush=True)

    # ===== 日报（stdout）=====
    print(f"📋 超跌反弹候选池 ReboundPool {today} | 市场{market_status}级({market_score_val}分)")
    if trend_core > 0:
        print(f"  趋势池当前 core {trend_core}只 → 本池仅作补充参考（同一时刻只开一个通道）")
    else:
        print(f"  趋势池当前为空 → 本池作为监控候选来源")
    if market_status == "D":
        print(f"  ⚠️ D市（禁买）：本池仅记录不交易")
    elif market_status == "C":
        print(f"  ⚠️ C市弱市：只监控 A 级，仓位减半纪律")
    print(f"候选链路: 全市场{len(all_stocks)} → 主板过滤{len(kept)} → 行业热度确认 → 精筛{len(candidates)} → 达标{len(results)}只")
    if hard_excl:
        ex = " ".join(f"{k}{v}" for k, v in hard_excl.most_common())
        print(f"硬排除: {ex}")
    print("──────────────────────────────")
    if a_lv:
        print(f"🎯 A级（重点监控，止跌已确认）{len(a_lv)}只:")
        for e in a_lv[:args.top]:
            d = e["detail"]
            ind = e['industry']
            print(f"  {e['code']} {e['name']} {e['score']}分(技术{e['tech_score']}) | {ind['name']} {ind['heat_reason']} "
                  f"| 回撤{d['dd20']}% RSI{d['rsi']} | {d['stabilize_signals']} | ATR{d['atr_pct']}%")
            print(f"     触发: 突破昨日高点{e['trigger']['watch']}关注 | 站稳MA5 {e['trigger']['ma5']} | "
                  f"跌破止跌低点{e['trigger']['stop_low']}放弃")
    else:
        print(f"🎯 A级: 无达标票（需 ≥75分 + 今日不创新低 + 至少1项止跌信号）")
    if b_lv:
        print(f"👀 B级（观察）{len(b_lv)}只:")
        for e in b_lv[:args.top]:
            d = e["detail"]
            ind = e['industry']
            print(f"  {e['code']} {e['name']} {e['score']}分(技术{e['tech_score']}) | {ind['name']} {ind['heat_reason']} "
                  f"| 回撤{d['dd20']}% RSI{d['rsi']} | 止跌{'✅' if e['stabilize_ok'] else '❌'}")
    else:
        print(f"👀 B级: 无")
    if c_lv:
        print(f"📌 C级（仅记录）{len(c_lv)}只:")
        for e in c_lv[:args.top]:
            d = e["detail"]
            ind = e['industry']
            print(f"  {e['code']} {e['name']} {e['score']}分(技术{e['tech_score']}) | {ind['name']} {ind['heat_reason']} "
                  f"| 回撤{d['dd20']}% RSI{d['rsi']}")
    print("──────────────────────────────")
    print("纪律: 本池仅提供监控候选；买入需盘中二次确认（突破昨日高点+量能+行业热度未转弱+大盘不弱）")
    print("      止损=买入价-1×ATR 或 止跌低点×0.97 取严格者；3日不弹降级；5日不达标退出")


if __name__ == "__main__":
    main()
