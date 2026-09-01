#!/usr/bin/env python3
"""观察池12只实时评分 + 全市场S级扫描"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_pool as sp
import stock_config

# ===== 1. 观察池12只评分 =====
print("=" * 70)
print("📋 观察池12只实时评分（收盘后）")
print("=" * 70)
# 基准指数
bench = sp.short_term.get_index_kline("sh000001", 30)
bench_chg20 = (float(bench[-1]["close"]) / float(bench[-21]["close"]) - 1) * 100 if bench and len(bench) >= 21 else 0
bench300 = sp.short_term.get_index_kline("sh000300", 30)
bench300_chg20 = (float(bench300[-1]["close"]) / float(bench300[-21]["close"]) - 1) * 100 if bench300 and len(bench300) >= 21 else 0

print(f"基准: 上证20日{bench_chg20:.2f}% 沪深300 20日{bench300_chg20:.2f}%")
for code, name, pref, hold in stock_config.STOCKS:
    e = sp.score_stock(code, name, 0, bench_chg20, bench300_chg20, amount=0, turnover=None)
    if e:
        f = e.get("factor", {})
        print(f"  {code} {name}: 个股{e.get('stock_score','?')}分 {e.get('level','?')}级 | 趋势{f.get('trend','?')} 动量{f.get('momentum','?')} 资金{f.get('capital','?')} RS{f.get('rs','?')} 风险{f.get('risk','?')}"
              f"{'  ⚠️' + e['_exclude'] if e.get('_exclude') else ''}")
    else:
        print(f"  {code} {name}: 评分失败")
    time.sleep(0.3)
