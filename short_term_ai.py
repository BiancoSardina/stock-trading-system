#!/usr/bin/env python3
"""
short_term_ai.py — 短线信号自动分析（含 LLM 模型降级链）

用途：cron no_agent 模式任务脚本（0925/1110/1330/1445 四个时段）
流程：
  1. 运行 short_term.py 生成技术分析数据（MA/RSI/MACD/布林/评分/竞价/买卖信号）
  2. 组装 AI 解读 prompt，调用 deepseek API：
     - 首选 deepseek-v4-flash（超时 150s）
     - 失败/过载/超时 → 切换 deepseek-chat（超时 240s）
     - 两个都失败 → 兜底：直接输出脚本原始技术数据（保证用户一定能收到内容）
  3. stdout 输出最终报告（由 cron 原样投递到 QQ）

stdout 只输出报告正文；诊断信息写 stderr（cron 仅在非零退出时带上 stderr）。
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import position_manager
import qq_send

# 模型降级链（用户指定，只用这两个）
MODELS = [
    {"name": "deepseek-v4-flash", "timeout": 150},
    {"name": "deepseek-chat", "timeout": 240},
]

API_URL = "https://api.deepseek.com/v1/chat/completions"


# ─────────────────────────── 配置读取 ───────────────────────────
def get_api_key():
    """从 config.yaml 读 deepseek api_key（优先环境变量）"""
    env_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        import yaml
        cfg_path = os.path.expanduser("~/.hermes/config.yaml")
        if os.path.exists(cfg_path):
            cfg = yaml.safe_load(open(cfg_path))
            key = (cfg.get("providers", {}).get("deepseek", {}) or {}).get("api_key", "")
            if key:
                return key
    except Exception as e:
        print(f"[warn] config.yaml 读取失败: {e}", file=sys.stderr)
    # 兜底：.env
    try:
        env_path = os.path.expanduser("~/.hermes/.env")
        if os.path.exists(env_path):
            for line in open(env_path):
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


# ─────────────────────────── 个股覆盖兜底 ───────────────────────────
def ensure_stock_coverage(text: str, data: str) -> str:
    """AI报告缺失个股时，把脚本里的【个股观察】区块原文补在报告末尾（硬性兜底，不依赖AI自觉）"""
    import re
    block, in_block = [], False
    for ln in data.split("\n"):
        if "【个股观察" in ln:
            in_block = True
        elif in_block and ln.strip().startswith("【") and "】" in ln:
            break
        if in_block:
            block.append(ln)
    if not block:
        return text
    missing = []
    for ln in block:
        m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9]{2,10})\((\d{6})\)", ln)
        if m and m.group(1) not in text:
            missing.append(m.group(1))
    if missing:
        extra = (
            f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 【个股数据补全】以下 {len(missing)} 只个股未在报告中解读，附脚本原文：\n"
            + "\n".join(block)
        )
        return text + extra
    return text


# ─────────────────────────── 时段指令 ───────────────────────────
def build_system_prompt(period: str) -> str:
    common = (
        "你是ETF投资分析助手，为A股短线交易者输出中文交易报告。\n"
        "铁律：\n"
        "1. 前瞻+预案式（禁止马后炮）：给条件单——回踩X买/突破Y追/破位Z走/反弹W减，"
        "不写\"现价已高于买入区今日放弃\"这类事后话术。\n"
        "2. 所有买卖建议必须带具体价格区间和手数，按A股规则整数倍：ETF按100份整数倍、个股按100股整数倍"
        "（如\"买入500份(约890元)\"）。\n"
        "2.5 【买点形态铁律·禁止情绪高位追入】（2026-08-14 昆药复盘新增，违反=不合格报告）：\n"
        "评分高≠买点好。标的处于以下任一形态时，禁止建议\"现价买入\"，必须改为条件单"
        "（回踩X企稳买/放量站稳Y再追），绝不喊现价建仓：\n"
        "  ① 昨日放量大涨(涨幅≥3%且量比≥1.5)且今日现价仍在日内高位未回踩到位——连涨追高风险；\n"
        "  ② 昨日放量(量比≥1.5)今日大跌(跌幅≤-1.5%)——上涨动能衰竭，出货嫌疑；\n"
        "  ③ 当日自高点回落≥3%——追入即套；\n"
        "  ④ 近3日累计涨幅≥12%（≈2个涨停）——情绪过热（低于12%的启动期强势股属正常，按①②③位置判断）。\n"
        "  例外：现价已回踩到日内低位（日内位置<40%）的强势股=回踩低吸位，正常给回踩买入预案"
        "（这是主用买点，不算追高，禁止误杀）。脚本输出中若出现\"买点形态不佳\"提示，必须按此铁律改条件单。\n"
        "__TRAPPED_RULE__\n"
        "4. 其他观察清单的ETF和个股正常给买卖信号，机会好可以交易。\n"
        "5. 当前实际持仓（脚本启动时动态读取自权威文件 positions.json，以此为准，不要假设其他持仓）："
        "__POSITIONS__。持仓处理规则见第10条退出系统；若某标的在持仓描述中不存在，一律按普通观察标的处理。\n"
        "5.5 【标的全覆盖·强制】脚本数据中的全部标的必须逐一覆盖，一只都不能漏："
        "（V1.7 筛选模式 2026-08-26：核心ETF池+观察ETF+个股观察+🧺股票池[core全部+watch综合分top5逐只，其余watch一行简略]+次日监测名单）"
        "每只至少给一句信号小结（现价+评分等级+方向）；**S/A级或信号共振明确的品种**给完整预案"
        "（买入区/追涨位/止损/目标位），其余（B级及以下/无信号）给触发价（站稳X/突破Y转多）一句话即可，不展开。"
        "每只持仓的名称必须逐只出现在报告中（名称+评分+一句话），报告缺持仓=不合格。\n"
        "5.6 【数据过滤·只报高等级】脚本数据已按 V1.5 过滤（2026-08-21 用户要求）+ V1.7 筛选（2026-08-26 方案A）："
        "非持仓且等级≤B（B/C/D）的固定自选/观察标的已整块剔除，不再出现在数据中——"
        "但🧺股票池来源的标的（core 全部 + watch 综合分top5）与当前持仓、A/S级强势标的**始终保留逐只输出**，不受等级过滤；"
        "股票池其余 watch 只给一行简略（代码/名称/总分），不展开。"
        "因此报告解读数据中出现的全部标的：①当前持仓（无论等级，必须覆盖）②A/S级强势标的③🧺股票池（core+watch top5 逐只）。"
        "禁止自行补入数据中没有的股票/ETF，也禁止猜测或脑补任何被过滤标的的行情；"
        "若某区块标题数量与内容一致即正常，不要问\"为什么只有这几只\"，直接按给出的标的分析。\n"
        "5.7 【长度控制·QQ分段友好】报告会按长度自动分2~3条QQ消息推送（每条限约1300汉字），"
        "请把报告总长控制在约3500个汉字以内：每只标的用一句话信号小结+关键价位，"
        "禁止重复表述、禁止冗余解释、禁止大段复述脚本数据；保证内容紧凑，整份报告2~3条消息发完。\n"
        "6. 不要编造任何消息面/新闻——只基于给出的技术数据判断；没有数据支撑的消息一律不写。\n"
        "7. 信号要果断明确；若确实无操作，明确写\"今日无操作，观望\"。\n"
        "7.5 【评分口径】脚本评分已升级为五因子模型 0~100 分：趋势30(价vsMA20/MA20vsMA60/MA60斜率/周线)+动量20(RSI+MACD+20日涨幅)+资金20(量能+换手率+主力净流入)+相对强度20(跑赢上证+跑赢沪深300+20日绝对强度)+风险10(ATR波动+20日最大回撤)。等级：S≥80 / A 65~79 / B 50~64 / C 35~49 / D<35(偏空减仓禁买)。解读评分时按此口径，不再是旧的±10分制。\n"
        "7.6 【手续费硬性规则·必须体现】A股佣金万2.5最低5元/笔(买卖双向)+卖出印花税0.05%(个股)+过户费万0.1：所有买卖建议必须考虑手续费成本。脚本输出中凡带⚠️手续费提醒的建议，说明该金额档位不划算(单笔<3000元占比≥0.17%)，必须在建议中明确提示并建议调整份额(资金允许时凑到3000元以上整手)；做T/短差建议必须确保价差覆盖双边手续费；禁止给小额买卖建议而不提示成本。\n"
        "8. 【买入区铁律·防止两头堵死循环】（最重要，违反=不合格报告）：\n"
        "   - 买入区必须是**当前趋势的动态支撑**：近5日低点、MA5、MA10、突破颈线/前高，取其中较高者附近；"
        "禁止把远离现价的前期低点/20日低点/起涨点当买入区——那些是\"趋势破坏位=放弃位\"，不是买点。\n"
        "   - 若现价已明显高于动态支撑（涨幅>5%）：禁止写\"等回踩前期低点\"这种永远等不到的废话；"
        "要么给**追涨预案**（放量突破近期高点/新高则追小仓，止损放突破位下方），要么给回踩MA5/MA10企稳再买。\n"
        "   - 每个可操作品种必须给完整**三段预案**并提前写死：①追涨触发价（放量突破X）②回踩买价（动态支撑Y企稳）"
        "③放弃条件（跌破Z=趋势破坏，明确写\"不接\"）。预案触发就按预案执行，禁止事后改口；"
        "禁止\"涨了说别追、跌了说偏空\"的两头堵——回踩到动态支撑就按预案买，只有破位才放弃。\n"
        "9. 【可执行性强制·宁缺毋滥】报告末尾必须附【📋 今日可挂单操作汇总】表，但**最多5行**，"
        "只放优先级最高的操作：优先S级>A级，B级仅在信号明确且市场允许时放；"
        "市场C/D级防守状态下禁止放入买入类操作；有疑点、趋势未确认、信号矛盾的品种一律不放。"
        "宁可只有1-2条甚至写\"今日无高优先操作\"，也绝不凑数——保证只做对的，不做错的。"
        "格式（标的名称 | 动作 | 挂单价 | 距现价% | 手数 | 止损价）——**标的必须写中文名称**"
        "（如\"中国卫星\"\"半导体设备ETF\"），**禁止用600118这类6位数字代码**；挂单价须在现价±5%内才允许入表；"
        "若品种所有触发价都距现价>5%，该品种不允许出现在汇总表（必须给出追涨方案后才允许）。"
        "任何\"观望/偏空/等企稳\"措辞必须同时附带转多触发价（站稳X/突破Y），否则视为未完成分析。\n"
        "10. 【四重退出系统·持仓必读】脚本对每个持仓输出【📤 退出系统】区块，含四种退出："
        "①止损退出(止损线=MA20/买价-2ATR/固定止损取最高，破线无条件清仓)；②趋势退出(盈利持仓上涨中不提前卖，"
        "收盘破MA10减仓50%、破MA20清仓)；③盈利保护(盈利≥10%激活，自买入后最高价回撤8%卖出)；"
        "④时间退出(持有≥10个交易日未上涨则卖)。解读规则：触发项(已触发/破线)必须给出明确卖出指令+手数，"
        "不得含糊；未触发项简述阈值即可，不重复展开；套牢盘(__TRAPPED_CODES__)的退出信号只作\"反弹减仓/做T\"提示，"
        "禁止清仓建议。持仓个股同样按四重退出执行（固定止损优先）。\n"
        "10.5 【ETF T策略区块·只解释不改价】数据中【ETF T策略】区块为引擎输出"
        "（状态机/T评分/卖出区/回补区），AI 只负责解释：当前状态含义、为什么等待/可卖/可回补、"
        "提醒用户按区块点位挂单执行；**禁止修改引擎给出的价格点位与手数**，AI 不决定T价格。"
        "引擎出现\"冲高形态触发\"文案=评分虽<70但日内曾冲高≥2.5%且现价仍≥+0.5%，属有效卖T信号"
        "（回落初期=卖出区现价附近直接高抛；回落确认=等反弹至卖出区再卖），正常解读执行；"
        "出现\"今日曾冲高X%已回落Y%，卖点错过；明日预案\"=今日已无肉可卖，明日再冲高≥2.5%时立即执行卖T。\n"
        "11. 【策略版本v2.31】"
        "18:15复盘会按版本对比胜率；虚拟交易跟踪(独立10万账户)自动验证信号有效性。"
        "解读时无需提及这些字段，正常输出报告即可。\n"
        "12. 【🎯 次日监测名单·重点跟踪】数据中的【次日监测名单】区块是最近一次选股任务"
        "AI裁决\"允许交易YES\"的标的（高风险短线候选），今天盘中必须重点跟踪："
        "①对照预案检查信号是否仍成立（趋势三要件/评分等级变化）；"
        "②给明确的今日操作预案——回踩X买/站稳Y追/破位Z走，带价格和手数；"
        "③若该标的已买入持仓（区块内显示持仓金额），按持仓管理并提示是否加仓；"
        "④尾盘必须给\"现价买/不买\"明确结论。注意：该名单会在当日尾盘轮次自动清理未买入的标的，"
        "今天是你最后的机会窗口，预案务必具体可执行。\n"
        "13. 【🧺 股票池·量化候选分析】数据中的【股票池】区块是最近一次五因子量化选股结果"
        "（当日早/午/尾盘生成；趋势/动量/资金/强度/风险+行业强度+位置扣分；core=总分≥门槛的升池候选，watch=观察池），"
        "**V1.7（2026-08-26 筛选方案A）core 全部 + watch 综合分 top5 逐只完整分析，其余 watch 只有一行简略（不展开）**："
        "逐只的每只至少一句信号小结（现价+评分等级+方向）；仅当评分S/A级且趋势三要件成立时才给完整预案，"
        "其余一句话带过（含触发价）。该池标的由当日AI裁决，YES票进次日监测名单。\n"
    )
    period_extra = {
        "早盘": (
            "【当前时段：早盘开盘后，开盘价已定、竞价量已出】\n"
            "本报告核心 = 基于集合竞价数据给出今日挂单预案（每个建议操作的品种都要给）。\n"
            "竞价强弱预案：\n"
            "- 高开≥2%（强势高开）：勿追开盘价！预案=等回踩（回踩位参考MA5/竞价低点/昨收附近）再买，"
            "或高开冲高回落企稳后再进；若持有可借高开做T高抛\n"
            "- 小幅高开0.5-2%（正常）：可竞价挂单或开盘小幅回踩买入，给出具体挂单价\n"
            "- 平开±0.5%：方向未定，预案=观望，等开盘15分钟方向明确再动，或挂支撑位低吸\n"
            "- 低开-2%以内：观察承接，预案=不抢反弹，等企稳信号（放量止跌）再进\n"
            "- 低开<-2%（弱势）：禁止买入抢反弹！持有者检查止损\n"
            "- 竞价放量(>30%×5日均量)+高开=强势信号；竞价缩量高开=虚高防回落\n"
            "预案格式（每个建议操作的品种）：挂单买入价格【X.XX】N份(约X元)；触发条件；止损跌破【Z.ZZ】不买/卖出。\n"
            "现价已高于建议买入区→明确写\"高开不追，等回踩【X.XX】挂单\"。\n"
            "为全天定调：先判断今天是什么日子（强势日/弱势日/震荡日）。注意10:00-11:00是量化收割时段，"
            "盘前挂单以低吸为主，不追高开冲高。最佳买入窗口在11:00后和14:00后。\n"
            "分析步骤：①集合竞价解读（高开/低开分布+竞价量能+大盘竞价强弱）②核心信号解读（A/B/S级信号、多指标共振）"
            "③前瞻判断（威科夫阶段、趋势三要件MA60、RS相对强度——只有趋势成立+RS居前才给买入预案）"
            "④预案式操作建议（核心输出）⑤持仓分析（按当前实际持仓+套牢盘做T预案）。"
        ),
        "收割后": (
            "【当前时段：11:10，上午收盘附近，量化收割(10:00-11:00)结束】\n"
            "量化收割结束，可观察捡漏。对超跌企稳的品种可给低吸预案；"
            "本报告核心 = 上午走势定性的午后预案（回踩X买/站稳Y追/破位Z走）。"
        ),
        "午后": (
            "【当前时段：13:30，午后开盘】\n"
            "午后行情延续，观察方向确认。给下午的预案（回踩X买/站稳Y追/破位Z走），"
            "提示尾盘再做最终决策，非尾盘不追高。"
        ),
        "尾盘": (
            "【当前时段：尾盘，今天最后操作窗口，15:00收盘】\n"
            "⚠️ 尾盘铁律：禁止\"回踩挂单\"\"等企稳再进\"这类废话——时间不够了！"
            "必须对每个品种给明确结论：现价直接买/不买（吃明日溢价），14:55前完成下单。\n"
            "看好就现价买，不挂单、不追尾盘急拉。"
        ),
    }
    common = common.replace("__POSITIONS__", position_manager.build_position_context())
    # 套牢盘名单动态注入（唯一来源：positions.json 的 no_sell 标记，禁止硬编码）
    _trapped = position_manager.trapped_codes()
    _pos_data = position_manager.load_positions()
    _trapped_names = "、".join(f"{p['name']}({p['code']})" for grp in ("etf", "stock")
                               for p in _pos_data.get(grp, []) if p["code"] in _trapped)
    if _trapped:
        _rule = ("3. 套牢盘处理（" + _trapped_names + "，必须遵守）：这些是套牢持仓，"
                 "禁止建议清仓/割肉离场；只允许做T和加仓摊低成本；做T必须遵守T+1"
                 "（先卖后买：高抛卖老仓→回落接回，接回的新仓次日才能再卖）；对这些只输出做T高抛/低吸点位+加仓点位。")
    else:
        _rule = ""
    common = common.replace("__TRAPPED_RULE__", _rule)
    common = common.replace("__TRAPPED_CODES__", "、".join(sorted(_trapped)) or "无")
    return common + "\n" + period_extra.get(period, "")


def build_user_prompt(data: str) -> str:
    return (
        "以下是定时任务脚本刚生成的技术分析数据（已算出MA、RSI、MACD、布林带、评分、买卖信号、"
        "集合竞价信息等）。请结合这些数据完成深度分析并输出报告（中文，结构清晰）。\n"
        "报告结构建议：大盘定调 → 核心信号解读 → 持仓处理（按当前实际持仓，无持仓则跳过）→ "
        "各品种操作预案（含买入/卖出价位区间）→ 今日总结。\n\n"
        "【技术分析数据】\n"
        + data
    )


# ─────────────────────────── LLM 调用 ───────────────────────────
def call_deepseek(model_name: str, timeout: int, api_key: str, sys_prompt: str, user_prompt: str):
    """调用 deepseek chat completions，返回文本；任何失败抛异常"""
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 16384,
        "stream": False,
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    try:
        content = body["choices"][0]["message"]["content"] or ""
        content = content.strip()
        if not content:
            raise RuntimeError(
                f"模型返回空内容 (finish_reason={body['choices'][0].get('finish_reason')}, "
                f"usage={body.get('usage', {})})"
            )
        return content
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError(f"响应解析失败: {str(body)[:300]}")


# ─────────────────────────── 主流程 ───────────────────────────
def main():
    api_key = get_api_key()
    if not api_key:
        print("⚠️ 【配置错误】未找到 deepseek API key，无法进行AI解读，以下为脚本原始数据：\n", file=sys.stderr)
    period = "早盘"
    try:
        period_map = {9: "早盘", 11: "收割后", 13: "午后", 14: "尾盘"}
        hour = time.localtime().tm_hour
        for h, p in sorted(period_map.items(), reverse=True):
            if hour >= h:
                period = p
                break
    except Exception:
        pass

    # 1) 生成技术数据
    script = os.path.join(SCRIPT_DIR, "short_term.py")
    try:
        _env = dict(os.environ)  # 2026-08-20 恢复全量：不设 HOLD_ONLY，核心池/观察ETF/个股/股票池全输出
        # V1.5（2026-08-21 用户要求：输出太多）：非持仓 B级及以下（B/C/D）不输出——
        # 不送AI分析、不推送QQ。持仓/A/S级保留。核心池+监测池同样过滤。
        _env["FILTER_MIN_GRADE"] = "A"
        proc = subprocess.run(
            [sys.executable, script],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=SCRIPT_DIR,
            env=_env,
        )
        if proc.returncode != 0:
            print(f"⚠️ 【脚本错误】short_term.py 退出码 {proc.returncode}\nstderr:\n{proc.stderr[:2000]}")
            sys.exit(1)
        data = proc.stdout.strip()
    except subprocess.TimeoutExpired:
        print("⚠️ 【脚本超时】short_term.py 300s 未完成，本次任务中止。")
        sys.exit(1)
    except Exception as e:
        print(f"⚠️ 【脚本执行失败】{e}")
        sys.exit(1)

    if not data:
        print("⚠️ 【脚本输出为空】short_term.py 无输出。")
        sys.exit(1)

    # 2) AI 解读（模型降级链）
    if api_key:
        sys_prompt = build_system_prompt(period)
        user_prompt = build_user_prompt(data)
        last_err = None
        for m in MODELS:
            t0 = time.time()
            try:
                text = call_deepseek(m["name"], m["timeout"], api_key, sys_prompt, user_prompt)
                print(f"[info] {period} AI解读成功: model={m['name']} 耗时={time.time()-t0:.0f}s", file=sys.stderr)
                report = ensure_stock_coverage(text, data)
                if not qq_send.push_or_stdout(report):  # 分段直发 QQ，失败才走 stdout 兜底
                    print(report)
                return
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
                print(f"[warn] {m['name']} 失败: HTTP {e.code} (body: {e.read()[:200]})", file=sys.stderr)
            except urllib.error.URLError as e:
                last_err = f"连接失败: {e.reason}"
                print(f"[warn] {m['name']} 失败: {last_err}", file=sys.stderr)
            except Exception as e:
                last_err = str(e)[:200]
                print(f"[warn] {m['name']} 失败: {last_err}", file=sys.stderr)
        # 两个模型都失败 → 兜底输出脚本原始数据
        report = (
            f"⚠️ 【AI解读暂时不可用】两个模型(deepseek-v4-flash / deepseek-chat)均连接失败或超时"
            f"（最后错误: {last_err}），以下为脚本原始技术数据（无AI解读，仅供参考）：\n\n{data}"
        )
        if not qq_send.push_or_stdout(report):
            print(report)
    else:
        # 无 key → 直接给原始数据
        report = f"⚠️ 【配置错误】未找到 deepseek API key，以下为脚本原始技术数据（无AI解读）：\n\n{data}"
        if not qq_send.push_or_stdout(report):
            print(report)


if __name__ == "__main__":
    main()
