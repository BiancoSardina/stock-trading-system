#!/usr/bin/env python3
"""股票池空池诊断（非破坏性）— 只精评成交额 top N，不写 stock_pool.json。

用法: python3 diag_stock_pool.py [N]    # N=精评只数，默认60（约90秒）
适用: 用户问"今天股票池怎么没有票/为什么C没分析出来票"时，先跑这个定位，
      不要重跑全量（9-13分钟 + 会覆盖生产 stock_pool.json）。

判定速查（2026-08-11 实战）:
  · 个股五因子≥85 的票 = 0  → 强者通道必空（前置条件第一步就不满足）
  · 综合分最高 < thr（C市85）→ core/watch 全空 = "宁缺毋滥"设计预期，不是故障
  · 硬排除清单全是高位票（偏MA20>12%/20日涨>30%）→ V1.3 收紧的正常效果：
    弱市里强势票全高位被排，剩票评分不足 → 空池是结构性结果
"""
import sys, time
sys.path.insert(0, '/home/ubuntu/.hermes/scripts')
import stock_scanner, industry_rank, stock_pool, short_term

N = int(sys.argv[1]) if len(sys.argv) > 1 else 60
t0 = time.time()

# ① 市场状态
try:
    short_term.market_score()
    m = short_term.MARKET or {}
    print(f"市场状态: {m.get('state')}级({m.get('score')}分)", flush=True)
except Exception as e:
    print("市场状态失败:", e, flush=True)

# ② 全市场 + 基础过滤
all_stocks = stock_scanner.fetch_all_stocks()
kept, dropped = stock_scanner.basic_filter(all_stocks)
print(f"全市场{len(all_stocks)} → 基础过滤后{len(kept)}只", flush=True)

# ③ 行业映射（force=False 用当天缓存，17:30 跑过则秒出）
imap = industry_rank.build_industry_map(force=False)
ind_scores = industry_rank.score_industries(all_stocks, imap)
ind_index = industry_rank.stock_industry_index(imap)
print(f"行业评分: {len(ind_scores)}个行业", flush=True)

# ④ 候选按成交额排序取 top N
candidates = []
for k in kept:
    ind_name = ind_index.get(k["code"], "")
    k["industry"] = ind_name
    k["industry_score"] = ind_scores.get(ind_name, {}).get("score", 0)
    candidates.append(k)
candidates.sort(key=lambda x: -x["amount"])
top = candidates[:N]
print(f"诊断精评目标: {len(top)}只（成交额top{N}）", flush=True)

# ⑤ 基准指数
bench_chg20 = bench300_chg20 = None
try:
    k1 = short_term.get_index_kline("sh000001", 30)
    k3 = short_term.get_index_kline("sh000300", 30)
    if k1 and len(k1) >= 21:
        bench_chg20 = round((float(k1[-1]["close"]) / float(k1[-21]["close"]) - 1) * 100, 2)
    if k3 and len(k3) >= 21:
        bench300_chg20 = round((float(k3[-1]["close"]) / float(k3[-21]["close"]) - 1) * 100, 2)
    print(f"基准: 上证20日{bench_chg20}% 沪深300二十日{bench300_chg20}%", flush=True)
except Exception as e:
    print("基准失败:", e, flush=True)

# ⑥ 精评（与 stock_pool.main 同函数，KLINE_SLEEP 防限流）
scored = []
for i, c in enumerate(top):
    try:
        e = stock_pool.score_stock(c["code"], c["name"], c["industry_score"],
                                   bench_chg20, bench300_chg20,
                                   amount=c.get("amount"), turnover=c.get("turnover"))
    except Exception as ex:
        print(f"  {c['code']} 评分异常: {ex}", flush=True)
        continue
    if e:
        e["industry"] = c["industry"]
        e["industry_score"] = c["industry_score"]
        if e.get("_exclude"):
            print(f"  [硬排除] {e['code']} {e['name']}: {e['_exclude']}", flush=True)
        else:
            scored.append(e)
    time.sleep(0.05)

print(f"\n===== 精评完成: 成功{len(scored)}只，耗时{time.time()-t0:.0f}s =====", flush=True)
scored.sort(key=lambda x: -x["total_score"])

def strong_pre(e):
    """与 stock_pool.generate_pool 的强者通道前置条件保持一致"""
    f = e.get("factor", {})
    return (e["stock_score"] >= 85 and e.get("industry_score", 0) >= 65
            and f.get("rs", 0) >= 15 and f.get("capital", 0) >= 18
            and bool(e["trend"]["above_ma20"]) and bool(e["trend"]["ma20_gt_ma60"]))

print("\n--- 个股五因子 top15 ---")
for e in scored[:15]:
    f = e.get("factor", {})
    tr = e["trend"]
    sp = "★STRONG" if strong_pre(e) else ""
    print(f"  {e['code']} {e['name']} total={e['total_score']} 个股{e['stock_score']} "
          f"行业{e['industry']}({e['industry_score']}) RS={f.get('rs')} 资金={f.get('capital')} "
          f"MA20上={tr['above_ma20']} MA20>MA60={tr['ma20_gt_ma60']} 位置扣={e['position']['deduct']} {sp}")

n_sp = sum(1 for e in scored if strong_pre(e))
n_core = sum(1 for e in scored if e["total_score"] >= 85 and e["stock_score"] >= 70
             and e.get("industry_score", 0) >= 55
             and e["trend"]["above_ma20"] and e["trend"]["ma20_gt_ma60"])
n_watch = sum(1 for e in scored if e["stock_score"] >= 60 and e["trend"]["above_ma20"])
print(f"\ntop{N} 中: 强者通道达标 {n_sp} 只 | 普通core达标(≥85) {n_core} 只 | watch达标(仅个股条件) {n_watch} 只")
hi = ["%s %s(%s)" % (e['code'], e['name'], e['stock_score']) for e in scored if e['stock_score'] >= 85]
print("个股≥85的票:", hi)
