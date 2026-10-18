#!/usr/bin/env python3
"""
板块/代码前缀策略（单一出口）
================================================
2026-10-17 用户要求：取消板块过滤，全 A 股（沪深主板 / 创业板 / 科创板 / 北交所）
全部进入扫描与股票池，不再只保留沪深主板。

历史背景：此前 basic_filter 用「主板白名单」硬滤 300/301/302、688/689、920，
理由是"用户未开通创业板/科创板/北交所权限"。现按用户指令解除。

回滚开关：环境变量 BOARD_FILTER=main 可一键恢复旧的主板白名单（只做沪深主板）。

提供的函数：
  is_allowed(code)        是否允许进入扫描/池（默认 True：全部 A 股）
  is_main_board(code)     是否为沪深主板（保留给需要主板口径的旧逻辑/测试）
  sina_prefix(code)       新浪行情/K线前缀：sh / sz / bj（北交所用 bj）
  em_secid(code)          东财 secid：1.xxx(沪) / 0.xxx(深、北交所)
  limit_pct(code)         涨跌幅限制（主板10 / 创业板科创板20 / 北交所30）
  board_name(code)        板块中文名（报告标注用）
"""
import os

MAIN_BOARD_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
CHINEXT_PREFIX = ("300", "301", "302")          # 创业板
STAR_PREFIX = ("688", "689")                    # 科创板
BJ_PREFIX = ("920", "43", "83", "87", "88")     # 北交所（920 新号段 + 4xx/8xx 老号段）
# 非 A 股（B 股、指数/基金由数据源 hs_a 已排除，这里兜底拦截）
NON_A_PREFIX = ("900", "200")

BOARD_FILTER = os.environ.get("BOARD_FILTER", "all").strip().lower()


def is_main_board(code):
    """沪深主板（600/601/603/605/000/001/002/003）"""
    return str(code).startswith(MAIN_BOARD_PREFIX)


def is_bj(code):
    return str(code).startswith(BJ_PREFIX)


def is_allowed(code):
    """是否允许进入扫描/股票池。默认放开全部 A 股。"""
    code = str(code)
    if len(code) != 6 or not code.isdigit():
        return False
    if code.startswith(NON_A_PREFIX):
        return False
    if BOARD_FILTER == "main":       # 回滚开关
        return is_main_board(code)
    return True


def sina_prefix(code):
    """新浪前缀：北交所 bj，沪市(5/6/9) sh，其余 sz。"""
    code = str(code)
    if is_bj(code):
        return "bj"
    return "sh" if code.startswith(("5", "6", "9")) else "sz"


def em_secid(code):
    """东财 secid 前缀：沪市 1.，深市/北交所 0.（北交所走 0. 号段）。"""
    code = str(code)
    return ("1." if code.startswith(("5", "6", "9")) and not is_bj(code) else "0.") + code


def limit_pct(code):
    """涨跌幅限制（%）：主板10，创业板/科创板20，北交所30。"""
    code = str(code)
    if is_bj(code):
        return 30.0
    if code.startswith(CHINEXT_PREFIX) or code.startswith(STAR_PREFIX):
        return 20.0
    return 10.0


def board_name(code):
    code = str(code)
    if is_bj(code):
        return "北交所"
    if code.startswith(CHINEXT_PREFIX):
        return "创业板"
    if code.startswith(STAR_PREFIX):
        return "科创板"
    if is_main_board(code):
        return "主板"
    if code.startswith(("5", "15", "16", "18")):
        return "ETF"
    return "其他"


if __name__ == "__main__":
    for c in ("600000", "000001", "300750", "301389", "688981", "920002", "830799", "159516", "510300"):
        print(c, board_name(c), sina_prefix(c), em_secid(c), limit_pct(c), "allowed=", is_allowed(c))
