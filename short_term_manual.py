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
    """兼容旧调用；精简报告不再强制逐只覆盖无操作标的。"""
    return text


# ─────────────────────────── 手动模式 prompt ───────────────────────────
from report_contract import BASE_PROMPT as SYSTEM_PROMPT


def build_position_context() -> str:
    """从权威持仓文件动态生成持仓描述（统一用 position_manager 公共实现）"""
    return position_manager.build_position_context()


def build_user_prompt(data: str) -> str:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"【手动执行 · 数据采集时间 {now}】\n"
        "以下是脚本刚生成的实时技术分析数据（已算出MA、RSI、MACD、布林带、评分、买卖信号、"
        "实时行情等，数据为最新价）。请严格按系统提示输出不超过1200字的最终决策摘要；"
        "只展开持仓风险和脚本批准的最多3个动作，无操作候选不逐只解释。\n\n"
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
        "max_tokens": 2048,
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
                from report_contract import compact_report
                report = compact_report(ensure_stock_coverage(text, data))
                if not qq_send.push_or_stdout(report):  # 分段直发 QQ，失败才走 stdout 兜底
                    print(report)
                return
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
                print(f"[warn] {m['name']} 失败: HTTP {e.code} (body: {e.read()[:200]})", file=sys.stderr)
            except urllib.error.URLError as e:
                last_err = f"连接失败: {e.reason}"
                print(f"[warn] {m['name']} 失败: {last_err}", file=sys.stderr)
            except qq_send.PartialDeliveryError:
                raise
            except Exception as e:
                last_err = str(e)[:200]
                print(f"[warn] {m['name']} 失败: {last_err}", file=sys.stderr)
        report = (f"⚠️ {time.strftime('%H:%M')}｜AI解读不可用（{last_err}）｜"
                  "本轮无可验证操作；完整技术数据已保留在后台，请勿依据旧报告下单。")
        if not qq_send.push_or_stdout(report):
            print(report)
    else:
        report = (f"⚠️ {time.strftime('%H:%M')}｜未配置AI密钥｜"
                  "本轮无可验证操作；完整技术数据已保留在后台。")
        if not qq_send.push_or_stdout(report):
            print(report)


if __name__ == "__main__":
    main()
