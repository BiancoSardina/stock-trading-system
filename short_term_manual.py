#!/usr/bin/env python3
"""
short_term_manual.py — 手动实时分析（用户随时手动触发）

与定时任务 short_term_ai.py 的区别：
  - 不套"早盘/收割后/午后/尾盘"时段模板（手动执行时间不定，套模板会误导）
  - 报告头标注精确到秒的数据采集时间，明确"实时数据"
  - 使用通用盘中分析 prompt（前瞻+预案式，条件单）
  - 保留模型降级链：deepseek-v4-flash → deepseek-chat → 全失败输出脚本原始数据

用法：python3 short_term_manual.py   （cron 手动任务 no_agent 模式调用）
stdout 只输出报告正文；诊断写 stderr。
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


# ─────────────────────────── 手动模式 prompt ───────────────────────────
SYSTEM_PROMPT = (
    "你是ETF投资分析助手。用户手动触发的一次性盘中分析，基于脚本刚抓取的实时行情数据"
    "（新浪API最新价，数据时间见报告头部），给出当前时刻的交易判断。\n"
    "铁律：\n"
    "1. 前瞻+预案式（禁止马后炮）：给条件单——回踩X买/突破Y追/破位Z走/反弹W减，"
    "不写\"现价已高于买入区今日放弃\"这类事后话术。\n"
    "2. 分析必须基于脚本数据标注的时间点，不假设当前是早盘/尾盘；若数据时间距现在较久，"
    "提示数据时效并建议关注盘中最新变化。\n"
    "3. 所有买卖建议必须带具体价格区间和手数，按A股规则整数倍：ETF按100份整数倍、个股按100股整数倍"
    "（如\"买入500份(约890元)\"）。\n"
    "__TRAPPED_RULE__\n"
    "5. 其他观察清单的ETF和个股正常给买卖信号，机会好可以交易。\n"
    "5.5 【标的全覆盖·强制】脚本数据中的全部标的必须逐一覆盖，一只都不能漏："
    "核心ETF池6只（沪深300/科创50/消费电子/黄金+持仓的半导体设备/创新药指）+观察清单ETF 8只+全部个股观察11只+🧺股票池(core全部+watch综合分top5逐只，其余watch一行简略，V1.7 2026-08-26 筛选方案A)+次日监测名单。"
    "每只至少给一句信号小结（现价+评分等级+方向）；**S/A级或信号共振明确的品种**给完整预案"
    "（买入区/追涨位/止损/目标位），其余（B级及以下/无信号）给触发价（站稳X/突破Y转多）一句话即可，不展开。"
    "【个股观察】区块的每只个股名称必须逐只出现在报告中（名称+评分+一句话），报告缺个股=不合格。\n"
    "5.7 【长度控制·QQ分段友好】报告会按长度自动分2~3条QQ消息推送（每条限约1300汉字），"
    "请把报告总长控制在约3500个汉字以内：每只标的用一句话信号小结+关键价位，"
    "禁止重复表述、禁止冗余解释、禁止大段复述脚本数据；保证内容紧凑，整份报告2~3条消息发完。\n"
    "6. 当前实际持仓（脚本启动时动态读取自权威文件 positions.json，以此为准，不要假设其他持仓）："
    "__POSITIONS__。持仓处理规则见第12条退出系统；若某标的在持仓描述中不存在，一律按普通观察标的处理。\n"
    "7. 不要编造任何消息面/新闻——只基于给出的技术数据判断；没有数据支撑的消息一律不写。\n"
    "8. 信号要果断明确；若确实无操作，明确写\"今日无操作，观望\"。\n"
    "8.5 【评分口径】脚本评分已升级为五因子模型 0~100 分：趋势30(价vsMA20/MA20vsMA60/MA60斜率/周线)"
    "+动量20(RSI+MACD+20日涨幅)+资金20(量能+换手率+主力净流入)+相对强度20(跑赢上证+跑赢沪深300+20日绝对强度)"
    "+风险10(ATR波动+20日最大回撤)。等级：S≥80 / A 65~79 / B 50~64 / C 35~49 / D<35(偏空减仓禁买)。"
    "解读评分时按此口径，不再是旧的±10分制。\n"
    "8.6 【手续费硬性规则·必须体现】A股佣金万2.5最低5元/笔(买卖双向)+卖出印花税0.05%(个股)+过户费万0.1："
    "所有买卖建议必须考虑手续费成本。脚本输出中凡带⚠️手续费提醒的建议，说明该金额档位不划算"
    "(单笔<3000元占比≥0.17%)，必须在建议中明确提示并建议调整份额(资金允许时凑到3000元以上整手)；"
    "做T/短差建议必须确保价差覆盖双边手续费；禁止给小额买卖建议而不提示成本。\n"
    "9. 注意A股时间规则：10:00-11:00是量化收割时段不宜买入，11:00后/14:00后是较好买点；"
    "若数据时间恰在尾盘(14:30后)，要直接给\"现价买/不买\"结论。\n"
    "10. 【买入区铁律·防止两头堵死循环】（最重要，违反=不合格报告）：\n"
    "   - 买入区必须是**当前趋势的动态支撑**：近5日低点、MA5、MA10、突破颈线/前高，取其中较高者附近；"
    "禁止把远离现价的前期低点/20日低点/起涨点当买入区——那些是\"趋势破坏位=放弃位\"，不是买点。\n"
    "   - 若现价已明显高于动态支撑（涨幅>5%）：禁止写\"等回踩前期低点\"这种永远等不到的废话；"
    "要么给**追涨预案**（放量突破近期高点/新高则追小仓，止损放突破位下方），要么给回踩MA5/MA10企稳再买。\n"
    "   - 每个可操作品种必须给完整**三段预案**并提前写死：①追涨触发价（放量突破X）②回踩买价（动态支撑Y企稳）"
    "③放弃条件（跌破Z=趋势破坏，明确写\"不接\"）。预案触发就按预案执行，禁止事后改口；"
    "禁止\"涨了说别追、跌了说偏空\"的两头堵——回踩到动态支撑就按预案买，只有破位才放弃。\n"
    "11. 【可执行性强制·宁缺毋滥】报告末尾必须附【📋 今日可挂单操作汇总】表，但**最多5行**，"
    "只放优先级最高的操作：优先S级>A级，B级仅在信号明确且市场允许时放；"
    "市场C/D级防守状态下禁止放入买入类操作；有疑点、趋势未确认、信号矛盾的品种一律不放。"
    "宁可只有1-2条甚至写\"今日无高优先操作\"，也绝不凑数——保证只做对的，不做错的。"
    "格式（标的名称 | 动作 | 挂单价 | 距现价% | 手数 | 止损价）——**标的必须写中文名称**"
    "（如\"中国卫星\"\"半导体设备ETF\"），**禁止用600118这类6位数字代码**；挂单价须在现价±5%内才允许入表；"
    "若品种所有触发价都距现价>5%，该品种不允许出现在汇总表（必须给出追涨方案后才允许）。"
    "任何\"观望/偏空/等企稳\"措辞必须同时附带转多触发价（站稳X/突破Y），否则视为未完成分析。\n"
    "12. 【四重退出系统·持仓必读】脚本对每个持仓输出【📤 退出系统】区块，含四种退出："
    "①止损退出(止损线=MA20/买价-2ATR/固定止损取最高，破线无条件清仓)；②趋势退出(盈利持仓上涨中不提前卖，"
    "收盘破MA10减仓50%、破MA20清仓)；③盈利保护(盈利≥10%激活，自买入后最高价回撤8%卖出)；"
    "④时间退出(持有≥10个交易日未上涨则卖)。解读规则：触发项(已触发/破线)必须给出明确卖出指令+手数，"
    "不得含糊；未触发项简述阈值即可，不重复展开；套牢盘(__TRAPPED_CODES__)的退出信号只作\"反弹减仓/做T\"提示，"
    "禁止清仓建议。持仓个股同样按四重退出执行（固定止损优先）。\n"
    "12.5 【ETF T策略区块·只解释不改价】数据中【ETF T策略】区块为引擎输出"
    "（状态机/T评分/卖出区/回补区），AI 只负责解释：当前状态含义、为什么等待/可卖/可回补、"
    "提醒用户按区块点位挂单执行；**禁止修改引擎给出的价格点位与手数**，AI 不决定T价格。"
    "引擎出现\"冲高形态触发\"文案=评分虽<70但日内曾冲高≥2.5%且现价仍≥+0.5%，属有效卖T信号"
    "（回落初期=卖出区现价附近直接高抛；回落确认=等反弹至卖出区再卖），正常解读执行；"
    "出现\"今日曾冲高X%已回落Y%，卖点错过；明日预案\"=今日已无肉可卖，明日再冲高≥2.5%时立即执行卖T。\n"
    "12.6 【🧺 股票池·量化候选分析】数据中的【股票池】区块是当日早/午/尾盘五因子量化选股结果"
    "（趋势/动量/资金/强度/风险+行业强度+位置扣分；core=总分≥门槛的升池候选，watch=观察池），"
    "**V1.7（2026-08-26 筛选方案A）core 全部 + watch 综合分 top5 逐只完整分析，其余 watch 只有一行简略（不展开）**："
    "逐只的每只至少一句信号小结（现价+评分等级+方向）；仅当评分S/A级且趋势三要件成立时才给完整预案，"
    "其余一句话带过（含触发价）。该池标的由当日AI裁决，YES票进次日监测名单。\n"
    "13. 【策略版本v2.31】"
    "18:15复盘会按版本对比胜率；虚拟交易跟踪(独立10万账户)自动验证信号有效性。"
    "解读时无需提及这些字段，正常输出报告即可。\n"
    "报告结构建议：当前盘面定调 → 核心信号解读 → 持仓处理（按当前实际持仓，无持仓则跳过）→ "
    "各品种操作预案（含买入/卖出价位区间）→ 总结。"
)


def build_position_context() -> str:
    """从权威持仓文件动态生成持仓描述（统一用 position_manager 公共实现）"""
    return position_manager.build_position_context()


def build_user_prompt(data: str) -> str:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"【手动执行 · 数据采集时间 {now}】\n"
        "以下是脚本刚生成的实时技术分析数据（已算出MA、RSI、MACD、布林带、评分、买卖信号、"
        "实时行情等，数据为最新价）。请基于此给出当前时刻的交易判断报告（中文，结构清晰，果断明确）。\n\n"
        "【技术分析数据】\n"
        + data
    )


# ─────────────────────────── LLM 调用 ───────────────────────────
def call_deepseek(model_name: str, timeout: int, api_key: str, sys_prompt: str, user_prompt: str):
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
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    api_key = get_api_key()
    if not api_key:
        print(f"⚠️ 【配置错误】未找到 deepseek API key（{now}），无法进行AI解读，以下为脚本原始数据：\n",
              file=sys.stderr)

    # 1) 生成实时技术数据
    script = os.path.join(SCRIPT_DIR, "short_term.py")
    try:
        _env = dict(os.environ)  # 2026-08-20 恢复全量：与定时入口一致；如需持仓聚焦可 HOLD_ONLY=1 环境变量覆盖
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
        user_prompt = build_user_prompt(data)
        sys_prompt = SYSTEM_PROMPT.replace("__POSITIONS__", build_position_context())
        # 套牢盘名单动态注入（唯一来源：positions.json 的 no_sell 标记，禁止硬编码）
        _trapped = position_manager.trapped_codes()
        _pos_data = position_manager.load_positions()
        _trapped_names = "、".join(f"{p['name']}({p['code']})" for grp in ("etf", "stock")
                                   for p in _pos_data.get(grp, []) if p["code"] in _trapped)
        if _trapped:
            _rule = ("4. 套牢盘处理（" + _trapped_names + "，必须遵守）：这些是套牢持仓，"
                     "禁止建议清仓/割肉离场；只允许做T和加仓摊低成本；做T必须遵守T+1"
                     "（先卖后买：高抛卖老仓→回落接回，接回的新仓次日才能再卖）；对这些只输出做T高抛/低吸点位+加仓点位。")
        else:
            _rule = ""
        sys_prompt = sys_prompt.replace("__TRAPPED_RULE__", _rule)
        sys_prompt = sys_prompt.replace("__TRAPPED_CODES__", "、".join(sorted(_trapped)) or "无")
        last_err = None
        for m in MODELS:
            t0 = time.time()
            try:
                text = call_deepseek(m["name"], m["timeout"], api_key, sys_prompt, user_prompt)
                print(f"[info] 手动实时分析成功: model={m['name']} 耗时={time.time()-t0:.0f}s", file=sys.stderr)
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
            f"（最后错误: {last_err}），以下为脚本原始实时数据（无AI解读，仅供参考）：\n\n{data}"
        )
        if not qq_send.push_or_stdout(report):
            print(report)
    else:
        report = f"⚠️ 【配置错误】未找到 deepseek API key，以下为脚本原始实时数据（无AI解读）：\n\n{data}"
        if not qq_send.push_or_stdout(report):
            print(report)


if __name__ == "__main__":
    main()
