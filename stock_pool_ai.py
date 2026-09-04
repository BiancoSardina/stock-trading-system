#!/usr/bin/env python3
"""
股票池V1.1 — 18:00 AI 裁决（stock_pool_ai.py）
==============================================
每日 18:00 运行：读 stock_pool.json 的 core_pool，对前 N 只做四角色
YES/NO 裁决 → 写入 watchlist.json（次日重点监测），报告发 QQ。

设计依据：stock_pool_design_v1.md §6/§7（V1.1）
  · core_pool 前 5~8 只 → 趋势分析师 → 风险经理 → 交易员 → 综合裁决
  · 综合裁决"允许交易: YES"的标的 → watchlist.json（复用代码兜底 extract_yes_stocks）
  · watch_pool 不裁决，直接进次日监控（short_term.py 读取 stock_pool.json 时处理）
  · 任务开始先 cleanup_watchlist（昨日未买入的监测标的移出）
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import watchlist
import position_manager
import qq_send
import stock_pool_manager as spm
from ai_decision import FINAL_CONTRACT, validate_verdict, render_verdict
from runtime import exclusive, data_path
import stock_picker_ai as spa  # 复用四角色prompt/LLM调用/watchlist兜底

CORE_AI_LIMIT = 8  # 只裁决 core 前8只（控制 4-5 分钟耗时）


def build_pool_data(pool, limit=CORE_AI_LIMIT):
    """core_pool 前 limit 只 → 结构化文本（喂给四角色 LLM）"""
    core = pool.get("core_pool", []) or []
    # V3.0 Phase 3：加载决策状态机（历史状态检查——监督员职责的 LLM 侧输入，
    # 让裁决委员会知道"这票上次说过什么"，防止与之前建议冲突/追涨杀跌）
    try:
        import decision_manager
        _states = decision_manager.load_states()
        _hist_all = decision_manager.load_history()
    except Exception:
        _states, _hist_all = {}, []
    lines = [
        f"【股票池候选 · {pool.get('market_status','?')}级市场({pool.get('market_score','?')}分)"
        f" · {pool.get('date','')}】",
        f"core_pool 共{len(core)}只，以下为评分前{min(limit, len(core))}只：",
    ]
    for i, e in enumerate(core[:limit], 1):
        lines.append("   价格证据：" + json.dumps({"price": e.get("price"), "levels": e.get("levels"), "quote_time": e.get("quote_time")}, ensure_ascii=False))
        f = e.get("factor", {})
        p = e.get("position", {})
        t = e.get("trend", {})
        lines.append(
            f"\n{i}) {e.get('name','')}({e.get('code','')}) | {e.get('industry','')}"
            f"(行业{e.get('industry_score','?')}分) | 综合{e.get('total_score','?')}分 {e.get('level','')}级"
        )
        lines.append(f"   五因子: 趋势{f.get('trend','?')}/30 动量{f.get('momentum','?')}/20 "
                     f"资金{f.get('capital','?')}/20 强度{f.get('rs','?')}/20 风险{f.get('risk','?')}/10")
        lines.append(f"   位置: 20日涨幅{p.get('rise20','?')}% 偏离MA20 {p.get('distance_ma20','?')}% "
                     f"RSI14={p.get('rsi14','?')} 位置扣{p.get('deduct',0)}")
        lines.append(f"   趋势三要件: 价>MA20{'✓' if t.get('above_ma20') else '✗'} "
                     f"价>MA60{'✓' if t.get('above_ma60') else '✗'} "
                     f"MA20>MA60{'✓' if t.get('ma20_gt_ma60') else '✗'}")
        lines.append(f"   在池: {e.get('first_seen','')}加入 第{e.get('days_in_pool',1)}天")
        reasons = e.get("reason", [])
        if reasons:
            lines.append(f"   理由: {'; '.join(reasons)}")
        # V3.0：决策状态机注入（历史状态检查）
        _st = _states.get(e.get("code", ""), {})
        if _st:
            _st_txt = f"   状态机: {_st.get('state','?')} ｜ 上次动作: {_st.get('last_action','?')}"
            if _st.get("last_action_time"):
                _st_txt += f"({str(_st.get('last_action_time'))[11:16]})"
            lines.append(_st_txt)
            _hist = [h for h in _hist_all if h.get("code") == e.get("code", "")]
            if _hist:
                _trail = " → ".join(
                    f"{h.get('old_action','?')}→{h.get('new_action','?')}" for h in _hist[-3:]
                )
                lines.append(f"   决策轨迹: {_trail}")
    lines.append("\n（以上为股票池生成时的评分数据，实际交易须重新检查实时行情。"
                 "你的职责：对每只给出允许交易 YES/NO 裁决与次日预案。"
                 "注意每只的【状态机】与【决策轨迹】——若建议与历史动作冲突，"
                 "必须说明理由（是否构成重大变化），禁止无理由推翻上次决策。）")
    return "\n".join(lines)


def _call_role(role_name, sys_prompt, data_prompt, extra_context=""):
    """单角色调用（模型降级链 v4-flash → deepseek-chat），返回文本或 None"""
    api_key = spa.get_api_key()
    user_prompt = data_prompt
    if extra_context:
        user_prompt += f"\n\n【上一环节分析（供参考，可质疑但须引用）】\n{extra_context}"
    last_err = None
    for m in spa.MODELS:
        t0 = time.time()
        try:
            text = spa.call_deepseek(m["name"], m["timeout"], api_key, sys_prompt, user_prompt)
            print(f"[info] {role_name}成功: model={m['name']} 耗时={time.time()-t0:.0f}s", file=sys.stderr)
            return text
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            print(f"[warn] {role_name} {m['name']} 失败: HTTP {e.code}", file=sys.stderr)
        except urllib.error.URLError as e:
            last_err = f"连接失败: {e.reason}"
            print(f"[warn] {role_name} {m['name']} 失败: {last_err}", file=sys.stderr)
        except Exception as e:
            last_err = str(e)[:200]
            print(f"[warn] {role_name} {m['name']} 失败: {last_err}", file=sys.stderr)
    print(f"[warn] {role_name} 全部模型失败(最后错误: {last_err})", file=sys.stderr)
    return None


def _with_watchlist(report, cleanup_lines, new_added):
    """报告末尾附加次日监测名单区块"""
    sep = "=" * 55
    parts = [f"\n{sep}\n🎯 【次日监测名单】（自动加入明日盘中监测）\n{sep}"]
    if cleanup_lines:
        parts.append("昨日名单处理：")
        parts.extend(f"  {l}" for l in cleanup_lines)
    if new_added:
        parts.append("今日新增监测（允许交易YES）：")
        for i in new_added:
            if isinstance(i, dict):
                c, n = i.get("code", ""), i.get("name", "")
            else:
                c, n = i
            parts.append(f"  ➕ {c} {n} 明日盘中重点跟踪")
    else:
        parts.append("今日无新增监测标的（无YES或AI未输出名单行）")
    cur = watchlist.summary()
    parts.append(f"当前名单：{cur}")
    return report + "\n" + "\n".join(parts)


def _run_four_roles(data_prompt, final_sys):
    """四角色裁决（降级链 v4-flash → deepseek-chat），返回 dict：
    {final, trend, risk, trader}（各自可能为 None）"""
    trend_out = _call_role("趋势分析师", spa.ROLE_TREND_SYSTEM, data_prompt)
    risk_out = _call_role("风险经理", spa.ROLE_RISK_SYSTEM, data_prompt,
                          f"【趋势分析师结论】\n{trend_out if trend_out else '（趋势分析师调用失败，请独立评估）'}")
    trader_out = _call_role("交易员", spa.ROLE_TRADER_SYSTEM, data_prompt,
                            f"【趋势分析师结论】\n{trend_out if trend_out else '（无）'}\n"
                            f"【风险经理评级】\n{risk_out if risk_out else '（风险经理调用失败，请独立评估）'}")
    final_out = _call_role("综合裁决", final_sys, data_prompt,
                           f"【趋势分析师】\n{trend_out if trend_out else '（无）'}\n"
                           f"【风险经理】\n{risk_out if risk_out else '（无）'}\n"
                           f"【交易员】\n{trader_out if trader_out else '（无）'}")
    return {"final": final_out, "trend": trend_out, "risk": risk_out, "trader": trader_out}


@exclusive(lambda: data_path("stock_pool_ai.run"))
def main():
    today = datetime.now().strftime("%Y-%m-%d")
    t0 = time.time()

    pool = spm.load_old_pool()
    if not pool or pool.get("date") != today or not pool.get("data_ok"):
        raise ValueError("股票池过期或数据未经完整性校验，停止AI裁决")
    generated = datetime.strptime(pool.get("generated_at", ""), "%Y-%m-%d %H:%M:%S")
    if not 0 <= (datetime.now() - generated).total_seconds() <= 1800:
        raise ValueError("股票池超过30分钟，重新生成后再裁决")
    cleanup_lines = []
    core = (pool or {}).get("core_pool", []) or []
    market_status = (pool or {}).get("market_status", "?")
    market_score_val = (pool or {}).get("market_score", "?")

    if not core:
        report = (f"⚠️ 【股票池AI裁决】今日无 core 候选"
                  f"（市场{market_status}级{market_score_val}分，门槛过高无达标票）——"
                  f"不裁决，无新增监测。\n"
                  f"watch_pool 若存在将由盘中任务按观察处理。")
        report = _with_watchlist(report, cleanup_lines, [])
        if not qq_send.push_or_stdout(report):
            print(report)
        print(f"[stock_pool_ai] 无core候选，报告已发 耗时{time.time()-t0:.0f}s", file=sys.stderr)
        return

    # 2) 构造候选数据 + 市场简报
    data = build_pool_data(pool)
    market_brief = spa.get_market_brief()
    data_prompt = (
        f"【股票池AI裁决 · {today}，以候选的生成时间为准】\n"
        f"以下是本轮股票池模块生成的 core 候选评分数据，请完成裁决并输出次日预案"
        f"（中文，结构清晰，信号果断）。\n"
        + (f"\n【市场环境评分】\n{market_brief}\n" if market_brief else "")
        + "\n【股票池候选数据】\n" + data
    )

    # 3) 四角色裁决（降级链）
    final_sys = spa.ROLE_FINAL_SYSTEM.split("【监测名单行")[0].replace("__POSITIONS__", position_manager.build_position_context()) + FINAL_CONTRACT
    r = _run_four_roles(data_prompt, final_sys)
    final_out, trend_out, risk_out, trader_out = r["final"], r["trend"], r["risk"], r["trader"]

    # 4) 输出报告：综合裁决优先；失败拼三角色；全失败兜底数据
    if final_out:
        # A valid NO is final; insertion count is never a reason to rerun the model.
        approved, decisions = validate_verdict(final_out, core[:CORE_AI_LIMIT], market_status)
        if os.environ.get("SKIP_CLEANUP") != "1":
            cleanup_lines, _ = spa.cleanup_watchlist()
        new_added = watchlist.add_stocks(approved, reason="v3.1结构化YES，盘中必须重新确认") if approved else []
        report = _with_watchlist(render_verdict(decisions, core[:CORE_AI_LIMIT]), cleanup_lines, new_added)
        report += f"\n本次允许监测{len(approved)}只，新增{len(new_added)}只。"
        if not qq_send.push_or_stdout(report):
            print(report)
        return
    parts = []
    for tag, out in (("趋势分析师", trend_out), ("风险经理", risk_out), ("交易员", trader_out)):
        if out:
            parts.append(f"## {tag}\n{out}")
    if parts:
        report = "⚠️ 【综合裁决调用失败，以下为分角色分析（无最终YES/NO裁决）】\n" + "\n\n".join(parts)
        report = _with_watchlist(report, cleanup_lines, [])
        if not qq_send.push_or_stdout(report):
            print(report)
        return
    report = (
        f"⚠️ 【AI解读暂时不可用】四角色均连接失败或超时，以下为股票池候选原始数据"
        f"（无AI解读，仅供参考）：\n{data}"
    )
    report = _with_watchlist(report, cleanup_lines, [])
    if not qq_send.push_or_stdout(report):
        print(report)


if __name__ == "__main__":
    main()
