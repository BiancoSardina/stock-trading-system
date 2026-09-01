#!/usr/bin/env python3
"""
晚间 20:00 收盘深度分析任务（2026-08-18 新增）
================================================
用户要求：
  ① 所有数据全部成功后才分析（数据完整性门槛）
  ② 有数据不成功 → 间隔20分钟重试（最多重试1次）
  ③ 分析完成后再推送（QQ，成功静默/失败stdout兜底）
  ④ 报告 = 市场定调 + 持仓明日预案 + 首板池次日接力预案 + 行业强度

数据步骤（全部必须成功才算一轮通过）：
  S1 全市场扫描（stock_scanner：全市场 + 基础过滤）
  S2 行业映射（industry_rank.build_industry_map，行业数≥40 才算成功）
  S3 行业评分（industry_rank.score_industries）
  S4 市场评分（short_term.market_score）
  S5 首板池扫描（first_board_pool.scan，产出 first_board_pool.json）
  S6 持仓分析（short_term.py HOLD_ONLY=1 全流程，含做T/止损/明日预案）
"""
import json
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import stock_scanner
import industry_rank
import short_term
import first_board_pool
import qq_send
import watchlist

RETRY_WAIT = 1200   # 20分钟
MAX_ROUNDS = 2      # 首跑 + 1次重试（20:00 / 20:20）


# ─────────────── 数据步骤（每步返回 (ok, info)） ───────────────

def step_market_scan():
    """S1 全市场扫描 + 基础过滤"""
    all_stocks = stock_scanner.fetch_all_stocks()
    if len(all_stocks) < 3000:
        return False, f"全市场仅{len(all_stocks)}只(<3000)"
    kept, dropped = stock_scanner.basic_filter(all_stocks)
    if len(kept) < 200:
        return False, f"基础过滤后仅{len(kept)}只(<200): {dropped}"
    return True, f"全市场{len(all_stocks)}只, 过滤后{len(kept)}只"


def step_industry_map():
    """S2 行业映射（容错版，行业数≥40）"""
    imap = industry_rank.build_industry_map(force=True)
    n = len(imap.get("industries", {}))
    if n < 40:
        return False, f"行业映射仅{n}个(<40)"
    return True, f"行业映射{n}个"


def step_industry_score(all_stocks):
    """S3 行业评分"""
    imap = industry_rank.load_industry_map()
    scores = industry_rank.score_industries(all_stocks, imap)
    if len(scores) < 20:
        return False, f"行业评分仅{len(scores)}个(<20)"
    return True, scores


def step_market_score():
    """S4 市场评分"""
    indices = short_term.get_indices()
    lines = short_term.market_score(indices)
    if not lines:
        return False, "市场评分无输出"
    return True, lines


def step_first_board():
    """S5 首板池扫描（要求全市场数据正常，0候选≠失败）"""
    try:
        ret = first_board_pool.scan()
        with open(first_board_pool.OUTPUT, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") != time.strftime("%Y-%m-%d"):
            return False, "首板池日期非今日"
        if ret is not None and not ret.get("ok", True):
            return False, ret.get("error", "首板池数据异常")
        if (ret is None or ret.get("all_count", 0) < 3000) and data.get("all_count", 0) < 3000:
            return False, f"首板池全市场数据异常({data.get('all_count')}只)"
        # V1.4（2026-08-21 登海教训）：首板池 S/A 级候选（含启动日候选）自动写入
        # watchlist.json → 次日盘中 short_term 自动覆盖分析并推送信号，
        # 不依赖用户翻报告（立霸 8-20 入池 → 8-21 盘中 S级信号 的链路自动化）。
        _write_fb_to_watchlist(data)
        return True, data
    except Exception as e:
        return False, f"首板池异常: {e}"


def _write_fb_to_watchlist(fb_data):
    """首板池/启动日候选 → watchlist 次日监测（S级全写，A级写前3，防名单爆炸）"""
    try:
        cands = (fb_data.get("candidates", []) or []) + (fb_data.get("launch_candidates", []) or [])
        if not cands:
            return
        mkt = fb_data.get("market_status", "?")
        # 市场闸门：C级以下不推首板接力（弱市涨停多为出货）
        if mkt in ("C", "D") and fb_data.get("market_score", 0) < 60:
            return
        s_list = [c for c in cands if c.get("grade") == "S"]
        a_list = [c for c in cands if c.get("grade") == "A"]
        picks = s_list + a_list[:3]
        items = [(c["code"], c["name"]) for c in picks if c.get("code")]
        if items:
            watchlist.add_stocks(items, reason=f"首板池{fb_data.get('market_status','?')}级次日接力")
    except Exception as e:
        print(f"[evening] 首板池写watchlist失败: {e}", file=sys.stderr)


def step_positions():
    """S6 持仓分析（short_term.py HOLD_ONLY=1 全流程）"""
    script = os.path.join(SCRIPT_DIR, "short_term.py")
    try:
        env = dict(os.environ)
        env["HOLD_ONLY"] = "1"
        proc = subprocess.run([sys.executable, script], capture_output=True,
                              text=True, timeout=400, cwd=SCRIPT_DIR, env=env)
        if proc.returncode != 0:
            return False, f"short_term.py 退出码{proc.returncode}"
        out = proc.stdout.strip()
        if len(out) < 500:
            return False, f"short_term.py 输出过短({len(out)}字符)"
        return True, out
    except subprocess.TimeoutExpired:
        return False, "short_term.py 超时(400s)"
    except Exception as e:
        return False, f"short_term.py 异常: {e}"


# ─────────────── 报告生成 ───────────────

def build_report(pos_text, fb_data, ind_scores):
    today = time.strftime("%Y-%m-%d")
    L = [f"🌙 晚间深度分析 {today}", "=" * 30]

    # 市场定调（从持仓分析文本提取【大盘】摘要，避免重复）
    L.append("\n📊 【市场定调】")
    grab, grabbed = False, 0
    for ln in pos_text.split("\n"):
        s = ln.strip()
        if s.startswith("📊 【大盘】"):
            grab = True
            continue
        if grab:
            if s.startswith("【") and "】" in s and "市场" in s:
                break  # 下一区块（市场环境评分）
            if s and grabbed < 3:
                L.append(f"  {s}")
                grabbed += 1
            elif s:
                break
    if not grabbed:
        L.append("  （大盘摘要提取失败，见下方持仓分析）")

    # 持仓明日预案（short_term HOLD_ONLY 完整输出）
    L.append("\n💼 【持仓明日预案】")
    L.append(pos_text[:3500])

    # 首板池次日接力
    L.append("\n🚀 【首板池·明日接力】")
    cands = fb_data.get("candidates", [])
    if not cands:
        L.append("  今日无首板候选（或全部低于A级门槛）")
    else:
        L.append(f"  今日首板 {len(cands)}只（S级{sum(1 for c in cands if c['grade']=='S')}/A级{sum(1 for c in cands if c['grade']=='A')}）"
                 f" 市场{fb_data.get('market_status','?')}级")
        for c in cands[:8]:
            L.append(f"\n  {c['grade']} {c['name']}({c['code']}) {c['score']}分")
            L.append(f"    今收{c['close']} 涨停{c['chg']:+.1f}% 额{c['amount']}亿 换手{c['turnover']:.1f}% 量比{c['vol_ratio']}")
            if c.get("industry"):
                L.append(f"    行业{c['industry']}")
            p = c.get("plan", {})
            L.append(f"    次日: {p.get('高开0-3%','')}")
            L.append(f"    止损: {p.get('止损','')}")
        if len(cands) > 8:
            L.append(f"\n  ...其余{len(cands)-8}只见 first_board_pool.json")
        L.append("\n  ⚠️ 首板接力铁律：高开>6%不追；-5%硬止损；第三天必卖")

    # 启动日候选（放量大阳未涨停，登海型，2026-08-21 V1.4）
    lc = fb_data.get("launch_candidates", [])
    if lc:
        L.append("\n🔥 【启动日候选·次日预案】（放量大阳未涨停，登海型）")
        L.append(f"  今日启动日 {len(lc)}只（S级{sum(1 for c in lc if c['grade']=='S')}/A级{sum(1 for c in lc if c['grade']=='A')}）")
        for c in lc[:6]:
            p = c.get("plan", {})
            L.append(f"\n  {c['grade']} {c['name']}({c['code']}) {c['score']}分 涨幅{c['chg']:+.1f}% 量比{c['vol_ratio']}")
            L.append(f"    回踩买: {p.get('回踩买入','')}")
            L.append(f"    追涨买: {p.get('突破追涨','')}")
            L.append(f"    止损: {p.get('止损','')}")
        if len(lc) > 6:
            L.append(f"\n  ...其余{len(lc)-6}只见 first_board_pool.json")
        L.append("\n  ⚠️ 启动日接力铁律：次日高开>5%不追；回踩企稳低吸；破位止损；3天内必了结")

    # 行业强度 top5
    L.append("\n🏭 【行业强度 TOP5】")
    if isinstance(ind_scores, dict):
        top = sorted(ind_scores.items(), key=lambda x: -x[1].get("score", 0))[:5]
        for name, s in top:
            L.append(f"  {name}: {s.get('score',0)}分 (涨{s.get('avg_chg',0):+.1f}% 红盘率{s.get('up_ratio',0)*100:.0f}%)")

    # AI裁决·次日重点监测（watchlist.json，股票池18:00/手动裁决产物）
    L.append("\n🎯 【AI裁决·次日重点监测】")
    try:
        with open(os.path.join(SCRIPT_DIR, "watchlist.json"), encoding="utf-8") as f:
            wl = json.load(f)
        stocks = wl.get("stocks", [])
        if stocks:
            today_added = [s for s in stocks if s.get("added") == today]
            L.append(f"  监测名单 {len(stocks)}只（今日新增{len(today_added)}只）：")
            for s in stocks:
                mark = "➕" if s.get("added") == today else "  "
                L.append(f"  {mark} {s['code']} {s['name']}（{s.get('reason','')}）")
            L.append("  ⚠️ 次日盘中重点跟踪：回调企稳低吸为主，不追高；单票≤15%仓位")
        else:
            L.append("  当前无监测标的（今日无YES或未裁决）")
    except Exception:
        L.append("  watchlist.json 读取失败（今日未裁决或文件缺失）")

    L.append("\n📌 明日操作纪律：持仓按预案执行；首板接力只在市场A/B级环境开仓；单票≤15%仓位")
    return "\n".join(L)


# ─────────────── 主流程 ───────────────

def main():
    t_start = time.time()
    last_fails = {}
    for rnd in range(MAX_ROUNDS):
        if rnd > 0:
            print(f"[evening] 第{rnd+1}轮重试（等待{RETRY_WAIT}s后开始）", file=sys.stderr)
            time.sleep(RETRY_WAIT)
        print(f"[evening] === 第{rnd+1}/{MAX_ROUNDS}轮 数据准备 ===", file=sys.stderr)

        ok, info = step_market_scan()
        print(f"[evening] S1 全市场扫描: {'✅' if ok else '❌'} {info}", file=sys.stderr)
        if not ok:
            last_fails["S1_全市场"] = info
            continue
        all_stocks_ctx = None  # 不跨轮传递（每轮重拉）

        ok, info = step_industry_map()
        print(f"[evening] S2 行业映射: {'✅' if ok else '❌'} {info}", file=sys.stderr)
        if not ok:
            last_fails["S2_行业映射"] = info
            continue

        all_stocks = stock_scanner.fetch_all_stocks()
        ok, ind_scores = step_industry_score(all_stocks)
        print(f"[evening] S3 行业评分: {'✅' if ok else '❌'}", file=sys.stderr)
        if not ok:
            last_fails["S3_行业评分"] = ind_scores
            continue

        ok, mkt = step_market_score()
        print(f"[evening] S4 市场评分: {'✅' if ok else '❌'}", file=sys.stderr)
        if not ok:
            last_fails["S4_市场评分"] = mkt
            continue

        ok, fb = step_first_board()
        print(f"[evening] S5 首板池: {'✅' if ok else '❌'} {fb.get('market_status','') if isinstance(fb, dict) else fb}", file=sys.stderr)
        if not ok:
            last_fails["S5_首板池"] = fb
            continue

        ok, pos = step_positions()
        print(f"[evening] S6 持仓分析: {'✅' if ok else '❌'}", file=sys.stderr)
        if not ok:
            last_fails["S6_持仓分析"] = pos
            continue

        # 全部成功 → 生成报告并推送
        report = build_report(pos, fb, ind_scores)
        if not qq_send.push_or_stdout(report):
            print(report)
        print(f"[evening] ✅ 全部数据成功，报告已推送（耗时{time.time()-t_start:.0f}s）", file=sys.stderr)
        return 0

    # 重试耗尽
    fail_text = "\n".join(f"  {k}: {v}" for k, v in last_fails.items())
    alert = (f"⚠️ 【晚间分析数据失败】{time.strftime('%Y-%m-%d %H:%M')} 重试{MAX_ROUNDS}轮仍失败：\n"
             f"{fail_text}\n\n明早盘前任务会自动重试；如需手动补跑：python3 evening_analysis.py")
    if not qq_send.push_or_stdout(alert):
        print(alert)
    print(f"[evening] ❌ 数据重试耗尽，已推送失败告警", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
