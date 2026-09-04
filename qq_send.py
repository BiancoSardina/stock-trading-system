#!/usr/bin/env python3
"""
qq_send.py — QQ Bot 消息分段直发工具（2026-08-06 用户要求：QQ限制单条消息长度，推送必须分段）

背景：Hermes cron 的 no_agent 投递把脚本 stdout 当一条消息发，QQ 单条消息限制
MAX_MESSAGE_LENGTH=4000（UTF-8 字节），长报告（如全覆盖分析）会被截断。
本工具按字节安全切分报告，通过 QQ Bot REST API 逐段直发，每段带 (i/N) 标记。

用法：
  python3 qq_send.py <文本或文件路径>       # 发一段/多段到配置的 QQ 用户
  python3 qq_send.py --stdin               # 从 stdin 读全文
  python3 qq_send.py --test                # 发一条测试消息

作为模块：
  import qq_send
  ok = qq_send.send_report(report_text)    # 分段发送，返回全部成功 True/False
"""
import json
import os
import sys
import time
import uuid
import hashlib
from runtime import data_path, atomic_json, read_json, file_lock
import urllib.request
import urllib.error

API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
# QQ 官方单条消息上限（UTF-8 字节）为 4000；每段用到 3900（留余量给 [i/N] 标记），
# 让常规报告落在 2~3 段内（用户 2026-08-06 要求：尽量分 2~3 段，不要刷屏）
MAX_BYTES = 3900
TOKEN_FILE = data_path(".qq_token_cache.json")

# 目标用户（cron deliver 的 chat_id / openid）
DEFAULT_OPENID = os.getenv("QQ_TARGET_OPENID", "")


def _load_env_secrets():
    """从 ~/.hermes/.env 读 QQ_APP_ID / QQ_CLIENT_SECRET"""
    app_id = os.getenv("QQ_APP_ID", "")
    secret = os.getenv("QQ_CLIENT_SECRET", "")
    if app_id and secret:
        return app_id, secret
    env_path = os.path.expanduser("~/.hermes/.env")
    try:
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("QQ_APP_ID="):
                app_id = line.split("=", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("QQ_CLIENT_SECRET="):
                secret = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return app_id, secret


def get_token(force=False) -> str:
    """获取 access_token（7200s 有效，带本地缓存）"""
    if not force and os.path.isfile(TOKEN_FILE):
        try:
            cache = json.load(open(TOKEN_FILE))
            if cache.get("expires_at", 0) > time.time() + 120:
                return cache["token"]
        except Exception:
            pass
    app_id, secret = _load_env_secrets()
    if not app_id or not secret:
        raise RuntimeError("QQ_APP_ID / QQ_CLIENT_SECRET 未配置（~/.hermes/.env）")
    payload = json.dumps({"appId": app_id, "clientSecret": secret}).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"token 接口返回异常: {data}")
    expires_in = int(data.get("expires_in", 7200))
    atomic_json(TOKEN_FILE, {"token": token, "expires_at": time.time() + expires_in})
    return token


def _send_chunk(content: str, token: str, msg_seq: int) -> bool:
    """发送单条 C2C 文本消息"""
    body = json.dumps({
        "content": content,
        "msg_type": 0,  # MSG_TYPE_TEXT（QQ 常量：0=文本 2=markdown 7=media）
        "msg_seq": msg_seq,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/v2/users/{DEFAULT_OPENID}/messages",
        data=body,
        headers={
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
            "User-Agent": "Hermes-Cron/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status == 200


def _hard_split_line(ln: str, max_bytes: int):
    """单行超长时按 UTF-8 字节硬切，避免切断多字节字符（中文安全）"""
    b = ln.encode("utf-8")
    parts = []
    while len(b) > max_bytes:
        cut = max_bytes
        while cut > 0 and (b[cut] & 0xC0) == 0x80:  # 落在多字节字符中间 → 前移到字符边界
            cut -= 1
        if cut <= 0:
            cut = 1
        parts.append(b[:cut].decode("utf-8"))
        b = b[cut:]
    if b:
        parts.append(b.decode("utf-8"))
    return parts


def split_report(text: str, max_bytes: int = MAX_BYTES):
    """按 UTF-8 字节数安全切分（优先换行边界，超长行字节硬切），返回段落列表"""
    text = (text or "").strip("\n")
    if not text.strip():
        return []
    lines = text.split("\n")
    chunks, cur, cur_bytes = [], "", 0
    for ln in lines:
        lb = len(ln.encode("utf-8"))
        # 单行本身就超长 → 先硬切该行
        if lb > max_bytes:
            if cur:
                chunks.append(cur)
                cur, cur_bytes = "", 0
            for piece in _hard_split_line(ln, max_bytes):
                if cur:
                    chunks.append(cur)
                cur, cur_bytes = piece, len(piece.encode("utf-8"))
            continue
        if cur_bytes + lb + 1 > max_bytes and cur:
            chunks.append(cur)
            cur, cur_bytes = "", 0
        cur += ln + "\n"
        cur_bytes += lb + 1
    if cur.strip():
        chunks.append(cur)
    return chunks


class PartialDeliveryError(RuntimeError):
    """Already-sent or ambiguous chunks must never trigger a whole-report resend."""


def send_report(text: str, openid=None, dry_run=False):
    global DEFAULT_OPENID
    openid = openid or DEFAULT_OPENID
    chunks = split_report(text)
    if not chunks:
        return True
    if len(chunks) > 1:
        chunks = [f"[{i}/{len(chunks)}] {chunk}" for i, chunk in enumerate(chunks, 1)]
    if dry_run:
        return True
    if not openid:
        raise ValueError("请配置QQ_TARGET_OPENID")
    path = data_path("delivery_receipts.json")
    key = hashlib.sha256((openid + "\n" + text).encode()).hexdigest()
    with file_lock(path):
        receipts = read_json(path, {})
        record = receipts.setdefault(key, {"created_at": time.time(), "chunks": chunks,
                                           "status": ["pending"] * len(chunks)})
        if any(status == "sending" for status in record["status"]):
            raise PartialDeliveryError("上次发送结果不确定；先核对回执，禁止自动整份重发")
        if all(status == "sent" for status in record["status"]):
            return True
        DEFAULT_OPENID = openid
        try:
            token = get_token()
        except Exception as exc:
            if "sent" in record["status"]:
                raise PartialDeliveryError("已有分段送达，凭据失败；保留回执等待补发") from exc
            return False
        seq = int(time.time()) % 65536
        for i, chunk in enumerate(chunks):
            if record["status"][i] == "sent":
                continue
            record["status"][i] = "sending"
            atomic_json(path, receipts)
            try:
                sent = _send_chunk(chunk, token, (seq + i) % 65536)
            except urllib.error.HTTPError as exc:
                record["status"][i] = "failed"
                record["error"] = f"HTTP {exc.code}"
                atomic_json(path, receipts)
                raise PartialDeliveryError("分段发送失败，回执已保存，仅补发未送达段") from exc
            except Exception as exc:
                # The server may have received it even if the response was lost.
                raise PartialDeliveryError("发送结果不确定，回执保留sending；请核对后再处理") from exc
            record["status"][i] = "sent" if sent else "failed"
            atomic_json(path, receipts)
            if not sent:
                raise PartialDeliveryError("部分报告未送达，回执已保存；禁止整份兜底重发")
            time.sleep(.4)
        return True


def push_or_stdout(report: str) -> bool:
    """
    报告接入函数：分段直发到 QQ。
    返回 True = 发送成功（调用方应输出空，让 cron 静默，避免重复投递）；
    返回 False = 发送失败（调用方应把原文输出到 stdout，由 cron 兜底投递）。
    环境变量 QQ_SEND_DISABLE=1 时强制返回 False（禁用 QQ 发送，报告走 stdout，
    用于"手动分析输出到控制台"场景）；定时任务不设置该变量，行为不变。
    """
    if os.getenv("QQ_SEND_DISABLE"):
        return False
    try:
        return send_report(report)
    except PartialDeliveryError:
        raise
    except Exception as e:
        print(f"[qq_send] 推送失败: {e}", file=sys.stderr)
        return False


def main():
    args = sys.argv[1:]
    if "--test" in args:
        ok = send_report("🧪 QQ 分段直发通道测试 OK\n如果收到本条消息，说明分段通道正常。")
        print("test send:", "OK" if ok else "FAILED", file=sys.stderr)
        sys.exit(0 if ok else 1)
    if "--stdin" in args:
        text = sys.stdin.read()
    elif args:
        p = args[0]
        if os.path.isfile(p):
            text = open(p, encoding="utf-8").read()
        else:
            text = p
    else:
        print("用法: python3 qq_send.py <文本|文件|--stdin|--test>", file=sys.stderr)
        sys.exit(2)
    ok = send_report(text)
    print("send_report:", "OK" if ok else "PARTIAL_FAILED", file=sys.stderr)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
