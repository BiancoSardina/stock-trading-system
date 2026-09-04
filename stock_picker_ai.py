#!/usr/bin/env python3
"""
stock_picker_ai.py — 个股挂单预案自动分析（含 LLM 模型降级链）+ 底部横盘低风险扫描合并

用途：cron no_agent 模式任务脚本（1800 收盘后为明日做挂单预案）
流程：
  1. 运行 stock_picker.py 生成全市场个股筛选数据（仅沪深主板 600/601/603/605/000/001/002/003，
     高风险短线候选精简为 2-3 只）
  2. 运行 bottom_scan.py --top 3 生成低风险底部横盘候选（3只左右），合并进同一份报告
  3. 组装 AI 解读 prompt，调用 deepseek API：
     - 首选 deepseek-v4-flash（超时 150s）
     - 失败/过载/超时 → 切换 deepseek-chat（超时 240s）
     - 两个都失败 → 兜底：直接输出脚本原始筛选数据
  4. stdout 输出最终报告（高风险挂单预案 + 低风险横盘候选，由 cron 原样投递到 QQ）

stdout 只输出报告正文；诊断信息写 stderr。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import watchlist  # 次日监测名单管理
import position_manager
import qq_send

# 模型降级链（用户指定，只用这两个）
MODELS = [
    {"name": "deepseek-v4-flash", "timeout": 150},
    {"name": "deepseek-chat", "timeout": 240},
]

API_URL = "https://api.deepseek.com/v1/chat/completions"


# ─────────────────────────── 配置读取 ───────────────────────────
def parse_watchlist_line(text: str):
    """
    从综合裁决报告末尾解析【监测名单】行。
    返回 [(code, name), ...]；未找到或"无"返回 None（表示不更新名单）。
    """
    if not text:
        return None
    # 取最后一个【监测名单】行
    matches = re.findall(r"【监测名单】\s*([^\n]*)", text)
    if not matches:
        return None
    content = matches[-1].strip()
    if content in ("无", "无操作", "无监测", "", "-"):
        return []
    items = []
    # 支持 "600111 北方稀土;002348 高乐股份" 或 "600111,002348"
    for seg in re.split(r"[;；,，]", content):
        seg = seg.strip()
        if not seg:
            continue
        m = re.match(r"(\d{6})\s*([^\s\d;；,，]*)", seg)
        if m:
            # 名称必须清洗：AI 常输出 "九洲药业**" 带 markdown 星号（2026-08-07 实战脏数据）
            items.append((m.group(1), _clean_name(m.group(2) or "")))
    return items


def cleanup_watchlist():
    """
    任务运行前清理监测名单：昨天(或更早)加入的标的，若今天仍未买入则移出。
    返回 (cleaned_report_lines, bought_codes)
    """
    try:
        _data = position_manager.load_positions()
        pos_codes = {p["code"] for grp in ("etf", "stock") for p in _data.get(grp, [])}
    except Exception as exc:
        raise ValueError("持仓事实不可用，保留原监测名单") from exc
    today = datetime.now().strftime("%Y-%m-%d")
    result = watchlist.clean_stale(pos_codes, today)
    lines = []
    for e in result["bought"]:
        lines.append(f"✅ {e['code']} {e['name']} 已买入持仓 → 转入持仓监控，移出监测名单")
    for e in result["expired"]:
        lines.append(f"🗑 {e['code']} {e['name']} 未买入 → 已移出监测名单（下次不再跟踪）")
    return lines, pos_codes


def _clean_name(raw: str) -> str:
    """清洗AI输出的标的名称：去掉 markdown 星号/括号/空白等杂质"""
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", raw or "")


def extract_yes_stocks(text: str) -> list:
    """
    代码级兜底（不依赖 AI 自觉）：从综合裁决报告中提取"允许交易: YES"的标的。
    命中格式（行内同时出现 名称(代码) 与 YES 标记，名称允许夹带 ** 等 markdown 杂质）：
      | **金诚信(603979)** | ✅ 成立 | 🔴高（可控） | **YES** | ...
      金诚信(603979) ... 允许交易: YES ...
      哈三联**（002900）... **YES**
    返回 [(code, name), ...]，去重保序；未命中返回 []。
    """
    if not text:
        return []
    items = []
    seen = set()
    for line in text.splitlines():
        clean = line.replace("*", "")
        if re.search(r"\bNO\b", clean, re.I) or not (re.search(r"允许交易\s*[:：]\s*YES\b", clean, re.I) or re.search(r"\|\s*YES\s*\|", clean, re.I)):
            continue
        m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9*#]{2,14}?)[（(]?\s*(\d{6})", line)
        if m:
            code = m.group(2)
            name = _clean_name(m.group(1))
            if not name:
                continue
            if code not in seen:
                seen.add(code)
                items.append((code, name))
    return items


def update_watchlist(final_out: str, candidates=None, market_state="UNKNOWN") -> list:
    """解析报告末行监测名单并写入 watchlist.json，返回本次新增条目。

    兜底策略：AI 严格输出了【监测名单】行 → 按行解析；
    AI 没输出该行（返回 None）→ 从报告中提取"允许交易: YES"的标的自动写入；
    AI 明确输出"无"（返回 []）→ 不写入。
    """
    if candidates is None:
        return []  # Legacy prose has no verifiable candidate identity or price evidence.
    from ai_decision import validate_verdict
    items, _ = validate_verdict(final_out, candidates, market_state)
    return watchlist.add_stocks(items) if items else []


def add_cond_monitor(core_pool: list, top_n: int = 5, min_score: float = 75.0) -> list:
    """V1.4（2026-08-21 登海教训）：裁决保守无 YES 时的【条件单监测】兜底。

    从 core 池中挑"趋势成立"（trend 三要件全满足）且 total_score 达标的高分标的，
    写入 watchlist.json 标注为条件单监测——次日盘中信号≥A级+放量突破前高即可买入，
    不再"仅跟踪"。解决登海 8-17 裁决 NO → 8-18 盘中 A级75 平开低吸点无人衔接的问题。

    返回本次新增条目列表；core 为空或全不达标返回 []。
    """
    if not core_pool:
        return []
    cands = []
    for p in core_pool:
        trend = p.get("trend") or {}
        if not (trend.get("above_ma20") and trend.get("above_ma60") and trend.get("ma20_gt_ma60")):
            continue  # 趋势未成立不强推
        try:
            score = float(p.get("total_score") or p.get("stock_score") or 0)
        except (TypeError, ValueError):
            score = 0
        if score < min_score:
            continue
        cands.append((p.get("code", ""), p.get("name", ""), score))
    if not cands:
        return []
    cands.sort(key=lambda x: -x[2])
    items = [(c, n) for c, n, _ in cands[:top_n] if c]
    if not items:
        return []
    return watchlist.add_stocks(items, reason="条件单监测：裁决保守，次日信号≥A级+放量突破前高可买入（登海教训修复）")


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


# ─────────────────────────── 1800 收盘后 prompt（四角色分离·第七阶段）───────────────────────────
# 不再用一个AI全包：趋势分析师 → 风险经理 → 交易员 → 综合裁决，四级递进、上下文传递

_COMMON_CONTEXT = (
    "现在是18:00收盘后，为下一个交易日的开盘做挂单预案。"
    "以下是脚本刚生成的全市场个股筛选数据（已用新浪行情接口计算MA、RSI、MACD、评分、止损位等，"
    "只含沪深主板个股：600/601/603/605/000/001/002/003；已排除创业板300/301、科创板688、北交所——"
    "用户未开通这些权限）。\n"
    "【数量纪律】高风险短线候选已精简为仅 2-3 只（短期2只+中期1只，宁缺毋滥）——"
    "只对评分最高、信号最共振的标的给预案，禁止为了凑数把平庸标的写进报告。\n"
    "【长度控制·QQ分段友好】报告会按长度自动分2~3条QQ消息推送（每条限约1300汉字），"
    "请把报告总长控制在约3500个汉字以内：每只候选一句话信号小结+关键价位，"
    "禁止重复表述、禁止冗余解释，整份报告2~3条消息发完。\n"
    "数据纪律：只基于给出的技术数据判断，禁止编造任何消息面/新闻。\n"
    "铁律：\n"
    "1. 前瞻+预案式（禁止马后炮）：给条件单——回踩X买/突破Y追/破位Z走/反弹W减。\n"
    "2. 【买入区铁律·防止两头堵死循环】（最重要，违反=不合格报告）：\n"
    "   - 买入区必须是**当前趋势的动态支撑**：近5日低点、MA5、MA10、突破颈线/前高，取其中较高者附近；"
    "禁止把远离现价的前期低点/起涨点当买入区——那些是\"趋势破坏位=放弃位\"，不是买点。\n"
    "   - 若现价已明显高于动态支撑（涨幅>5%）：禁止写\"等回踩前期低点\"这种永远等不到的废话；"
    "要么给**追涨预案**（放量突破近期高点/新高则追小仓，止损放突破位下方），要么给回踩MA5/MA10企稳再买。\n"
    "   - 每个建议操作的品种必须给完整**三段预案**并提前写死：①追涨触发价（放量突破X）②回踩买价（动态支撑Y企稳）"
    "③放弃条件（跌破Z=趋势破坏，明确写\"不接\"）。预案触发就按预案执行，禁止事后改口；"
    "禁止\"涨了说别追、跌了说偏空\"的两头堵——回踩到动态支撑就按预案买，只有破位才放弃。\n"
    "3. 所有买卖建议必须带具体价格和手数，按A股规则100股整数倍（如\"买入100股(约1391元)\"）。\n"
    "3.3 【买点形态铁律·禁止情绪高位追入】（2026-08-14 昆药复盘新增，违反=不合格报告）：\n"
    "评分高≠买点好。候选标的处于以下任一形态时，禁止给\"现价买入\"预案，必须改为条件单"
    "（回踩X企稳买/放量站稳Y再追）：① 昨日放量大涨(≥3%且量比≥1.5)且今日现价仍在日内高位未回踩到位；"
    "② 昨日放量今日大跌(≤-1.5%)动能衰竭；③ 当日自高点回落≥3%追入即套；④ 近3日累计涨幅≥12%(≈2个涨停)情绪过热"
    "例外：现价已回踩到日内低位(位置<40%)的强势股=回踩低吸位，正常给回踩买入预案，禁止误杀。\n"
    "3.5 【手续费硬性规则·必须体现】A股佣金万2.5最低5元/笔(买卖双向)+卖出印花税0.05%(个股)+过户费万0.1："
    "所有挂单预案必须考虑手续费成本，单笔<3000元占比≥0.17%不划算——预案金额不足3000元时，"
    "必须明确提示并建议加大到3000元以上整手(资金允许时)；禁止给小额买卖建议而不提示成本。\n"
    "4. 当前实际持仓（脚本启动时动态读取自权威文件 positions.json，以此为准）：__POSITIONS__——"
    "给明日继续持有/加仓/止损/做T的明确预案；不在持仓描述中的标的一律按普通候选处理，不预设持有。\n"
    "5. 威科夫阶段判断（吸筹/拉升/派发/下跌）+ 趋势三要件（MA60向上+价在MA60上+MA20在MA60上）"
    "+ RS相对强度（跑赢大盘才算强）：只有\"趋势成立+RS居前\"的标的才给买入预案。\n"
    "6. 注意明日10:00-11:00是量化收割时段，挂单以低吸为主，不追高开冲高；最佳买入窗口在11:00后和14:00后。\n"
    "7. 【市场环境闸门·最高优先级】用户提示词中会附【市场环境评分】区块（含市场状态A/B/C/D和允许仓位）：\n"
    "    - D级(禁止交易)：一律只给观望/持仓/止损预案，禁止出现任何买入/挂买单/加仓建议（写\"市场D级禁止开新仓，只处理止损\"）；\n"
    "    - C级(防守)：允许买入但手数减半、只给回踩低吸预案、不给追涨预案；\n"
    "    - B级(正常)：买入手数×0.75，追涨预案标注\"仅轻仓\"；\n"
    "    - A级(强势)：正常出预案。\n"
    "    若市场评分区块缺失（数据异常），按B级处理。"
)
_COMMON_CONTEXT = _COMMON_CONTEXT.replace("__POSITIONS__", position_manager.build_position_context())

# 角色1：趋势分析师 — 只回答"趋势是否成立"
ROLE_TREND_SYSTEM = (
    "你是【趋势分析师】，A股短线交易团队的一员。你的唯一职责：判断每只候选标的的趋势是否成立。\n"
    + _COMMON_CONTEXT + "\n"
    "你的输出规范（只输出趋势判断，不要给买卖建议、不要给手数）：\n"
    "对每只候选标的分行输出：\n"
    "【标的】名称(代码)\n"
    "趋势三要件：MA60斜率(+x.xx%) / 价在MA60上/下 / MA20在MA60上/下 → 成立/不成立\n"
    "威科夫阶段：吸筹/拉升/派发/下跌（判断依据一句话）\n"
    "RS相对强度：20日涨幅+xx.x% vs 大盘（跑赢/跑输）→ 强势/弱势\n"
    "周线：多头/空头/纠缠\n"
    "趋势结论：✅成立 / ❌不成立（一句话理由）\n"
    "只对趋势成立且RS强势的标的给出✅；其余一律❌并说明缺哪条。"
)

# 角色2：风险经理 — 只回答"最大风险是什么"
ROLE_RISK_SYSTEM = (
    "你是【风险经理】，A股短线交易团队的一员。你的唯一职责：指出每只候选标的最大的风险，并把风险量化。\n"
    + _COMMON_CONTEXT + "\n"
    "你已收到趋势分析师的趋势结论（可能包含✅❌标记），请在此基础上独立评估风险。\n"
    "你的输出规范（只输出风险判断，不要给买卖建议）：\n"
    "对每只候选标的分行输出：\n"
    "【标的】名称(代码)\n"
    "最大风险：一句话说清（如：破MA20后下方无支撑/追高接盘/量价背离/大盘环境拖累）\n"
    "风险量化：\n"
    "  止损距离：买入价到止损位约 -x.x%\n"
    "  20日最大回撤：-xx.x%\n"
    "  市场环境：A/B/C/D级（影响仓位上限）\n"
    "风险等级：🔴高 / 🟠中 / 🟡低（判断标准：止损距离<-8%或回撤<-20%→高；市场D级禁止新增买入，C级仅低吸）\n"
    "一句话风险提示（给交易员看）。"
)

# 角色3：交易员 — 只回答"怎么买、多少钱"
ROLE_TRADER_SYSTEM = (
    "你是【交易员】，A股短线交易团队的一员。你的唯一职责：把趋势分析师✅的标的变成可执行的买卖计划。\n"
    + _COMMON_CONTEXT + "\n"
    "你已收到趋势分析师的趋势结论和风险经理的风险评级，请只对【趋势成立+风险可控】的标的设计预案。\n"
    "你的输出规范（每只建议操作的标的）：\n"
    "【标的】名称(代码) 现价X.XX\n"
    "① 追涨触发价：放量突破【X.XX】→ 买入N股(约X元)，止损【X.XX下方】\n"
    "② 回踩买价：回踩【X.XX】(动态支撑)企稳 → 买入N股(约X元)，止损【X.XX】\n"
    "③ 放弃条件：跌破【X.XX】→ 明确写\"不接\"\n"
    "目标位：反弹到【T.TT】止盈\n"
    "仓位：按市场级别缩放（D级禁买/C级减半/B级×0.75/A级正常），单票≤总资金30%，金额给整数手\n"
    "风险等级标注（继承风险经理评级）。\n"
    "末尾附【📋 明日可挂单操作汇总】表：标的名称（禁止用6位数字代码） | 动作 | 挂单价 | 距收盘价% | 手数 | 止损价。"
    "挂单价必须在收盘价±5%内才允许入表。若无可操作标的，明确写\"明日无操作，观望\"。"
)

# 角色4：综合裁决 — 允许交易 YES/NO
ROLE_FINAL_SYSTEM = (
    "你是【交易决策委员会主席】，A股短线交易团队的最后裁决者。"
    "你已收到三位成员的分析：趋势分析师（趋势结论）、风险经理（风险评级）、交易员（买卖预案）。\n"
    "你的唯一职责：给出最终综合报告，并对每只候选标的给出明确的\"允许交易: YES/NO\"。\n"
    "裁决规则：\n"
    "- 趋势❌ 或 风险🔴高 或 市场D级 → 允许交易: NO（并写明原因）\n"
    "- 趋势✅ 且 风险🟠中/🟡低 且 市场A/B/C级 → 允许交易: YES（引用交易员的买卖预案）\n"
    "- 综合报告结构：\n"
    "  一、市场定调（A/B/C/D级 + 一句话）\n"
    "  二、明日允许交易清单（每只：允许交易: YES/NO + 理由一句话 + 关键价位）\n"
    "  三、持仓处理（当前实际持仓由脚本动态注入：__POSITIONS__；不在其中的标的一律按普通候选处理，不预设持有。无持仓则写\"无持仓\"）\n"
    "  四、【📋 明日可挂单操作汇总】表\n"
    "  五、风控底线（总仓位上限、单票止损≤5%等）\n"
    "信号果断明确；若确实无操作，明确写\"明日无操作，观望\"。\n"
    "【监测名单行·机器可读·必须遵守】报告**最后一行**必须单独输出次日监测名单，格式严格为：\n"
    "【监测名单】601179 中国西电;002970 锐明技术\n"
    "规则：只列入\"允许交易: YES\"的标的（代码 名称，用分号分隔）；"
    "若全部 NO 则原样输出【监测名单】无。此行用于自动加入次日盘中监测，务必准确，不得省略、不得写在这行之后再输出任何内容。"
)


def get_market_brief() -> str:
    """获取市场环境评分摘要（复用 short_term.py 的 market_score），失败返回空串"""
    try:
        sys.path.insert(0, SCRIPT_DIR)
        import short_term
        return "\n".join(short_term.market_score())
    except Exception as e:
        print(f"[warn] 市场评分获取失败(按B级处理): {e}", file=sys.stderr)
        return ""


def build_user_prompt(data: str, market_brief: str = "") -> str:
    now = time.strftime("%Y-%m-%d %H:%M")
    brief_seg = f"\n【市场环境评分】\n{market_brief}\n" if market_brief else ""
    return (
        f"【收盘后预案 · 生成时间 {now}，为下一个交易日挂单】\n"
        "以下是脚本刚生成的全市场个股筛选数据。请结合数据完成深度分析并输出明日挂单预案报告"
        "（中文，结构清晰，信号果断）。\n"
        + brief_seg
        + "\n【个股筛选数据】\n"
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
    api_key = get_api_key()
    if not api_key:
        print("⚠️ 【配置错误】未找到 deepseek API key，无法进行AI解读，以下为脚本原始数据：\n",
              file=sys.stderr)

    # 0) 清理昨日监测名单（已买入→转入持仓监控；未买入→移出）
    cleanup_lines, _pos_codes = cleanup_watchlist()

    # 1) 生成个股筛选数据
    script = os.path.join(SCRIPT_DIR, "stock_picker.py")
    try:
        proc = subprocess.run(
            [sys.executable, script],
            capture_output=True,
            text=True,
            timeout=420,
            cwd=SCRIPT_DIR,
        )
        if proc.returncode != 0:
            print(f"⚠️ 【脚本错误】stock_picker.py 退出码 {proc.returncode}\nstderr:\n{proc.stderr[:2000]}")
            sys.exit(1)
        data = proc.stdout.strip()
    except subprocess.TimeoutExpired:
        print("⚠️ 【脚本超时】stock_picker.py 420s 未完成，本次任务中止。")
        sys.exit(1)
    except Exception as e:
        print(f"⚠️ 【脚本执行失败】{e}")
        sys.exit(1)

    if not data:
        print("⚠️ 【脚本输出为空】stock_picker.py 无输出。")
        sys.exit(1)

    # 1.5) 低风险底部横盘候选（3只左右），与高风险挂单预案合并成同一份报告
    bottom_data = ""
    bottom_script = os.path.join(SCRIPT_DIR, "bottom_scan.py")
    try:
        proc2 = subprocess.run(
            [sys.executable, bottom_script, "--top", "3"],
            capture_output=True,
            text=True,
            timeout=480,
            cwd=SCRIPT_DIR,
        )
        if proc2.returncode == 0 and proc2.stdout.strip():
            bottom_data = proc2.stdout.strip()
        else:
            print(f"[warn] bottom_scan.py 退出码 {proc2.returncode}: {proc2.stderr[:300]}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("[warn] bottom_scan.py 480s 超时，本次跳过低风险横盘部分", file=sys.stderr)
    except Exception as e:
        print(f"[warn] bottom_scan.py 执行失败: {e}", file=sys.stderr)

    def _with_bottom(report: str) -> str:
        """在报告末尾追加低风险底部横盘候选部分（若有）"""
        if bottom_data:
            sep = "=" * 55
            return f"{report}\n\n{sep}\n🟢 【低风险·底部横盘候选】(3只左右，突破震荡上沿放量可介入/跌破下沿放弃)\n{sep}\n{bottom_data}"
        return report

    def _with_watchlist(report: str, new_added: list) -> str:
        """在报告末尾追加监测名单状态（清理结果+今日新增）"""
        sep = "=" * 55
        parts = [f"\n{sep}\n🎯 【次日监测名单】（自动加入明日盘中监测）\n{sep}"]
        if cleanup_lines:
            parts.append("昨日名单处理：")
            parts.extend(f"  {l}" for l in cleanup_lines)
        if new_added:
            parts.append("今日新增监测（允许交易YES）：")
            for i in new_added:
                # 兼容 add_stocks 返回的 dict 条目（{code,name,added,reason}）与 (code,name) 元组
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

    # 2) AI 解读（四角色分离·第七阶段，模型降级链）
    #    流程：趋势分析师 → 风险经理 → 交易员 → 综合裁决（YES/NO）
    if api_key:
        market_brief = get_market_brief()
        data_prompt = build_user_prompt(data, market_brief)

        def _call_role(role_name: str, sys_prompt: str, extra_context: str = ""):
            """调用单角色（带模型降级链），返回文本或 None"""
            user_prompt = data_prompt
            if extra_context:
                user_prompt += f"\n\n【上一环节分析（供参考，可质疑但须引用）】\n{extra_context}"
            last_err = None
            for m in MODELS:
                t0 = time.time()
                try:
                    text = call_deepseek(m["name"], m["timeout"], api_key, sys_prompt, user_prompt)
                    print(f"[info] {role_name}成功: model={m['name']} 耗时={time.time()-t0:.0f}s", file=sys.stderr)
                    return text
                except urllib.error.HTTPError as e:
                    last_err = f"HTTP {e.code}"
                    print(f"[warn] {role_name} {m['name']} 失败: HTTP {e.code} (body: {e.read()[:200]})", file=sys.stderr)
                except urllib.error.URLError as e:
                    last_err = f"连接失败: {e.reason}"
                    print(f"[warn] {role_name} {m['name']} 失败: {last_err}", file=sys.stderr)
                except Exception as e:
                    last_err = str(e)[:200]
                    print(f"[warn] {role_name} {m['name']} 失败: {last_err}", file=sys.stderr)
            print(f"[warn] {role_name} 全部模型失败(最后错误: {last_err})", file=sys.stderr)
            return None

        # 角色1：趋势分析师
        trend_out = _call_role("趋势分析师", ROLE_TREND_SYSTEM)
        # 角色2：风险经理（参考趋势结论）
        risk_out = _call_role("风险经理", ROLE_RISK_SYSTEM,
                              f"【趋势分析师结论】\n{trend_out if trend_out else '（趋势分析师调用失败，请独立评估趋势）'}")
        # 角色3：交易员（参考趋势+风险）
        trader_out = _call_role("交易员", ROLE_TRADER_SYSTEM,
                                f"【趋势分析师结论】\n{trend_out if trend_out else '（无，独立判断）'}\n"
                                f"【风险经理评级】\n{risk_out if risk_out else '（风险经理调用失败，请独立评估风险）'}")
        # 角色4：综合裁决（汇总三角色）——系统提示词动态注入实际持仓（禁止硬编码已清仓标的）
        final_sys = ROLE_FINAL_SYSTEM.replace("__POSITIONS__", position_manager.build_position_context())
        final_out = _call_role("综合裁决", final_sys,
                               f"【趋势分析师】\n{trend_out if trend_out else '（无）'}\n"
                               f"【风险经理】\n{risk_out if risk_out else '（无）'}\n"
                               f"【交易员】\n{trader_out if trader_out else '（无）'}")

        # 输出最终报告：优先综合裁决；综合失败但三角色有产出 → 拼装三角色结果；全失败 → 兜底原始数据
        if final_out:
            new_added = update_watchlist(final_out)  # 解析监测名单行并写入 watchlist.json
            report = _with_watchlist(_with_bottom(final_out), new_added)
            if not qq_send.push_or_stdout(report):  # 分段直发 QQ，失败才走 stdout 兜底
                print(report)
            return
        parts = []
        for tag, out in (("趋势分析师", trend_out), ("风险经理", risk_out), ("交易员", trader_out)):
            if out:
                parts.append(f"## {tag}\n{out}")
        if parts:
            report = "⚠️ 【综合裁决调用失败，以下为分角色分析（无最终YES/NO裁决）】\n" + "\n\n".join(parts)
            report = _with_watchlist(_with_bottom(report), [])
            if not qq_send.push_or_stdout(report):
                print(report)
            return
        report = (
            f"⚠️ 【AI解读暂时不可用】四角色均连接失败或超时"
            f"（最后错误: {last_err if 'last_err' in dir() else '未知'}），以下为脚本原始筛选数据"
            f"（无AI解读，仅供参考）：\n{data}"
        )
        report = _with_watchlist(_with_bottom(report), [])
        if not qq_send.push_or_stdout(report):
            print(report)
    else:
        report = _with_watchlist(_with_bottom(data), [])
        if not qq_send.push_or_stdout(report):
            print(report)


if __name__ == "__main__":
    main()
