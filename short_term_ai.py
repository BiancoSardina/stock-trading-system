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
    from report_contract import build_report_prompt
    return build_report_prompt(period, position_manager.build_position_context())


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
