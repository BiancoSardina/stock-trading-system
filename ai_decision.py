"""Fail-closed validation of model verdicts against supplied candidates."""
import json
from runtime import positive

FINAL_CONTRACT = '''
最终仅输出一个JSON对象，不输出Markdown代码块：
{"decisions":[{"code":"600000","decision":"NO","reason":"证据说明"}]}
必须对输入中每只候选各裁决一次，代码不可增删或重复。decision只能为YES或NO。
YES须另外提供entry_ref（price/support/resistance之一）、entry和stop，数值必须逐字取自
该候选price或levels中的对应值；stop取levels.stop。数据缺失、stop>=entry、止损距离>8%、
市场D或UNKNOWN均必须NO。NO是有效最终裁决，不需要寻找替代买入对象。
'''


def validate_verdict(text, candidates, market_state):
    obj = json.loads(text)
    if not isinstance(obj, dict) or not isinstance(obj.get("decisions"), list):
        raise ValueError("最终裁决缺少decisions列表")
    allowed = {entry["code"]: entry for entry in candidates}
    decisions = obj["decisions"]
    seen, approved = set(), []
    for item in decisions:
        if not isinstance(item, dict):
            raise ValueError("裁决条目类型错误")
        code, decision = item.get("code"), item.get("decision")
        if code not in allowed or code in seen or decision not in ("YES", "NO"):
            raise ValueError("未知代码、重复代码或非YES/NO结论")
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError("裁决必须有理由")
        seen.add(code)
        if decision == "NO":
            continue
        candidate = allowed[code]
        levels = candidate.get("levels") or {}
        ref = item.get("entry_ref")
        expected = candidate.get("price") if ref == "price" else levels.get(ref) if ref in ("support", "resistance") else None
        entry, stop = item.get("entry"), item.get("stop")
        if not all(positive(x) for x in (entry, stop, expected, levels.get("stop"))):
            raise ValueError("YES缺少可核验价位")
        entry, stop = float(entry), float(stop)
        if abs(entry - float(expected)) > 1e-8 or abs(stop - float(levels["stop"])) > 1e-8:
            raise ValueError("AI价位与输入不一致")
        if not 0 < (entry - stop) / entry <= .08 or market_state not in ("A", "B", "C"):
            raise ValueError("YES违反风险门槛")
        approved.append((code, candidate.get("name", code)))
    if seen != set(allowed):
        raise ValueError("裁决未覆盖全部输入候选")
    return approved, decisions


def render_verdict(decisions, candidates):
    names = {e["code"]: e.get("name", e["code"]) for e in candidates}
    lines = ["📋 AI最终裁决（价格已经代码核验；实际动作仍需盘中状态机确认）"]
    for item in decisions:
        label = "允许监测" if item["decision"] == "YES" else "不允许交易"
        line = f"{names[item['code']]}({item['code']})：{label}。{item['reason']}"
        if item["decision"] == "YES":
            line += f" 参考价{item['entry']}，止损{item['stop']}。"
        lines.append(line)
    return "\n".join(lines)
