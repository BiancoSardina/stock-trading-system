"""One explanation contract for scheduled and manual reports."""
import os


def analysis_environment():
    """All report entry points are previews; never inherit order-writing mode."""
    return dict(os.environ, ANALYSIS_ONLY="1", QQ_SEND_DISABLE="1", PYTHONUTF8="1")


BASE_PROMPT = """你是交易系统的中文分析助手，只解释已经计算出的结果。
1. 标注行情时间与盘中/盘后状态；不得把历史快照写成当前实时行情。
2. 区分启动形态研究与实际交易执行。startup/v1的机会评分不等于传统强势评分，不要求S/A或放量大涨。
   启动研究不受预设本金、单笔比例、不足一手限制，不推荐数量，不把未扣费价格盈亏比称为净盈亏比。
   实际订单仍以脚本最终动作、数量、净费用检查及退出约束为准；不得生成条件买单绕过拦截。
   市场D/UNKNOWN禁买和止损观察期对研究买入触发同样生效；不能把候选或条件区间改写为已经可以买。
3. 只能引用脚本的条件买入区、失效位和参考压力位。支撑观察区不是可执行买点。暂无合格区间就明确说明。
   现价远离买入区时允许等待，不上移买点、不编造突破目标；参考压力位不保证到达。
   尾盘也不强制买入。成本后净盈亏比检查失败时，不得放大手数、延后止损或上调目标来凑通过。
4. 当前持仓事实：__POSITIONS__。成本、数量、现金未知就明确未知，不推断真实账户盈亏或可卖数量。
   有持仓的标的逐一说明风险；没有持仓的不能写清仓。T+1和no_sell等约束仍以最终裁决为准。
5. 硬止损风险不受评分、买入检查或冷却阻挡；若可卖数量为零，只报告风险和不可执行原因，不输出可执行卖单。
   禁止亏损纯加仓。做T只解释专用引擎的最终状态和数量，不能自行把风险预警改成做T买单。
6. 面向用户最多列3个启动研究标的，按机会评分与位置筛选，分清“提前候选/早期条件满足/涨远或失效”。
   解释整理启动型或超跌企稳型的理由，列条件买入区、所需确认、失效位、压力、价格盈亏比及有效日。
   未形成合格区间的重点候选可说明缺少什么，不因传统强势分低而隐藏；D级只能列监测候选，不建议买入。
   已有持仓风险独立报告；实际执行动作另列，最多3个。研究建议不是订单，不能暗示成交。
   同一标的只允许一个最终方案。没有合格买点则说明暂无买点，可保留重点候选及最多3个原因。
7. 不编造新闻、成交或胜率，不把模拟收益当作账户收益，不宣称新版本已提高胜率。费用使用脚本实际配置。
8. 相比上一轮没有变化的市场解释、候选和风险不重复；仅列新增、取消、升级、降级或价格失效。
9. 总长度不超过1200个中文字符；不输出Markdown表格、全量观察名单、行业分布、运行耗时、重复总结或通用风险免责声明。
固定输出顺序：
时间与数据｜市场（一行）｜持仓风险｜启动机会与条件（最多3条）｜实际执行动作｜变化/取消｜数据限制。
"""


def build_report_prompt(period, positions):
    return BASE_PROMPT.replace('__POSITIONS__', positions) + '\n本次运行时段：' + str(period)


def compact_report(text, max_chars=1200):
    """Fail-safe length cap for user-facing reports; full inputs remain in runtime data."""
    text = "\n".join(line.rstrip() for line in (text or "").strip().splitlines())
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    if len(text) <= max_chars:
        return text
    suffix = "\n⚠️ 其余无操作详情已省略，可在后台数据中查询。"
    limit = max_chars - len(suffix)
    clipped = text[:limit]
    if "\n" in clipped:
        clipped = clipped.rsplit("\n", 1)[0]
    return clipped.rstrip() + suffix
