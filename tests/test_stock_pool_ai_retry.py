#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stock_pool_ai 无YES自动重试逻辑单测（mock LLM/watchlist，不真实调用）"""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
import stock_pool_ai as spai
import stock_picker_ai as spa
import stock_pool_manager as spm
import qq_send

PASS, FAIL = 0, 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

# ── 通用 mock（无网络/无真实LLM/禁发QQ） ──
spa.cleanup_watchlist = lambda: ([], set())
spm.load_old_pool = lambda: {"market_status": "C", "market_score": 63, "date": "2026-08-07",
                             "core_pool": [{"code": "600001", "name": "票A", "industry": "行业A",
                                            "industry_score": 80, "total_score": 88, "level": "S",
                                            "first_seen": "2026-08-07", "days_in_pool": 1,
                                            "factor": {}, "position": {}, "trend": {},
                                            "reason": []}]}
spa.get_market_brief = lambda: ""
spai.spa.cleanup_watchlist = spa.cleanup_watchlist  # main 里用 spa.cleanup_watchlist
import position_manager
position_manager.build_position_context = lambda: ""
qq_send.push_or_stdout = lambda text: False  # 禁发 → print 到 stdout

# 可控的 _call_role 与 update_watchlist
ROLE_TEXT = {
    "趋势分析师": "趋势✅ 全部成立",
    "风险经理": "风险🟡 可控",
    "交易员": "低吸预案",
    "综合裁决": "综合裁决：允许交易 YES\n【监测名单】600001 票A",
}
CALL_LOG = []
def fake_call(role_name, sys_prompt, data_prompt, extra_context=""):
    CALL_LOG.append(role_name)
    return ROLE_TEXT.get(role_name)

UW_RESULTS = []  # update_watchlist 返回值队列（按调用顺序弹出）
def fake_update_watchlist(final_out):
    if UW_RESULTS:
        return UW_RESULTS.pop(0)
    return []

def run_main():
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        spai.main()
    return buf.getvalue()

print("== 场景1：首次裁决有 YES → 不重试 ==")
CALL_LOG.clear()
UW_RESULTS[:] = [[{"code": "600001", "name": "票A", "added": "2026-08-07", "reason": "t"}]]
spai._call_role = fake_call
spa.update_watchlist = fake_update_watchlist
out = run_main()
check("只调用一轮四角色", CALL_LOG.count("综合裁决") == 1, str(CALL_LOG))
check("报告含新增监测", "今日新增监测" in out and "票A" in out, "ok")
check("无重试标注", "自动重试" not in out, "ok")

print("== 场景2：首次无 YES，重试有 YES → 用重试结果 ==")
CALL_LOG.clear()
UW_RESULTS[:] = [[], [{"code": "600002", "name": "票B", "added": "2026-08-07", "reason": "t"}]]
out = run_main()
check("重试触发（两轮四角色）", CALL_LOG.count("综合裁决") == 2, str(CALL_LOG))
check("报告用重试结果(票B)", "票B" in out, "ok")
check("带重试标注", "自动重试" in out, "ok")

print("== 场景3：两次都无 YES → 接受无监测，标注保留 ==")
CALL_LOG.clear()
UW_RESULTS[:] = [[], []]
out = run_main()
check("重试触发两轮", CALL_LOG.count("综合裁决") == 2, str(CALL_LOG))
check("报告标注无新增", "无新增监测" in out, "ok")

print("== 场景4：综合裁决 LLM 失败 → 拼三角色，不重试 ==")
CALL_LOG.clear()
UW_RESULTS[:] = [[{"code": "600001", "name": "票A", "added": "2026-08-07", "reason": "t"}]]
def fake_call_fail(role_name, sys_prompt, data_prompt, extra_context=""):
    CALL_LOG.append(role_name)
    if role_name == "综合裁决":
        return None
    return ROLE_TEXT.get(role_name)
spai._call_role = fake_call_fail
out = run_main()
check("综合裁决失败不重试", CALL_LOG.count("综合裁决") == 1, str(CALL_LOG))
check("报告为分角色分析", "综合裁决调用失败" in out, "ok")
spai._call_role = fake_call

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
