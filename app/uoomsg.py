"""
uoomsg 接码模块
===============
API 文档：https://uoomsg.com → 查看 API 文档
基础地址：https://api.uoomsg.com/zc/data.php（GET，UTF-8）

关键实测：
  - cardType=实卡 并不能完全过滤虚拟号，实测取到 167 开头号码
  - 必须自己判号段：167/162/165/170/171/1740-1749 一律拒绝
  - 成功返回值是纯文本号码（11位，不带+86）
  - 失败返回值以 ERROR: 开头
  - 尚未收到短信：返回包含 "[尚未收到]"
  - keyWord 腾讯科技（URL编码）

虚拟号段（工信部批复给虚拟运营商，截至2026）：
  162,165,167,170,171 开头的11位号
  174 开头 0-9子段中 1740-1749 全部
  注：170/171 部分子号段被实体运营商收回，但仍以实测为准——
  这些号段腾讯服务端发码会直接失败，一律过滤。
"""
from __future__ import annotations

import re
import time
from typing import Any

import httpx

BASE = "https://api.uoomsg.com/zc/data.php"
KEYWORD = "腾讯科技"

# 虚拟号段前三位前缀（11位手机号判断）
VIRTUAL_PREFIXES = {
    "162", "165", "167",
    "170", "171",
    "173",   # 实测腾讯不往 173 发短信（queryUsed 无记录）
    "174",   # 174x 全部虚拟（1740-1749）
}


def _is_virtual(phone: str) -> bool:
    """判断是否虚拟号段（纯11位数字，不带+86）"""
    p = re.sub(r"\D", "", phone)
    if p.startswith("86") and len(p) == 13:
        p = p[2:]
    if len(p) != 11:
        return False
    return p[:3] in VIRTUAL_PREFIXES


def _call(token: str, params: dict[str, str], timeout: int = 20) -> str:
    """调用 uoomsg API，返回原始文本响应"""
    params["token"] = token
    with httpx.Client(timeout=timeout) as c:
        r = c.get(BASE, params=params)
    return r.text.strip()


def balance(token: str) -> float | str:
    """查余额，成功返回 float，失败返回错误字符串"""
    raw = _call(token, {"code": "leftAmount"})
    try:
        return float(raw)
    except ValueError:
        return raw


def get_phone(token: str, max_attempts: int = 10) -> dict[str, Any]:
    """
    取一个实体卡号码。
    会过滤虚拟号段，最多重试 max_attempts 次。
    返回 {"ok": True, "phone": "13xxxxxxxxx"} 或 {"ok": False, "error": "..."}
    """
    tried_virtual = []
    for i in range(max_attempts):
        raw = _call(token, {
            "code": "getPhone",
            "keyWord": KEYWORD,
            "cardType": "实卡",
        })
        if raw.startswith("ERROR:"):
            return {"ok": False, "error": raw}
        if not re.fullmatch(r"\d{11}", raw):
            return {"ok": False, "error": f"号码格式异常: {raw!r}"}
        if _is_virtual(raw):
            tried_virtual.append(raw)
            # 立即释放，让号池回收
            release(token, raw)
            time.sleep(0.5)
            continue
        return {"ok": True, "phone": raw, "skipped_virtual": tried_virtual}
    return {
        "ok": False,
        "error": f"连续 {max_attempts} 次都取到虚拟号段，暂停",
        "virtual_seen": tried_virtual,
    }


def query_used(token: str) -> list[dict[str, str]]:
    """
    查最近 24h 历史记录（每分钟最多1次，否则账号被封）。
    返回 [{"phone": "...", "content": "..."}, ...]
    """
    raw = _call(token, {"code": "queryUsed"}, timeout=30)
    if raw.startswith("ERROR:"):
        return []
    results = []
    for line in raw.splitlines():
        parts = line.split("\t", 2)
        if len(parts) >= 3:
            results.append({"phone": parts[0].strip(), "content": parts[2].strip()})
    return results


def get_sms(token: str, phone: str, timeout_s: int = 300,
            poll_interval: int = 70) -> dict[str, Any]:
    """
    等待腾讯验证码短信，最多 timeout_s 秒。
    返回 {"ok": True, "code": "123456", "raw": "全文"} 或 {"ok": False, "error": "..."}

    策略：完全依赖 queryUsed（getMsg 实测对所有号码无效，始终返回"尚未收到"）。
      - 发短信后先等 30s（腾讯短信下行延迟）
      - 之后每 poll_interval（默认70s）调一次 queryUsed（平台限速1次/分钟）
      - 每次查到目标手机号就提取验证码
    """
    phone_clean = re.sub(r"\D", "", phone)
    if phone_clean.startswith("86") and len(phone_clean) == 13:
        phone_clean = phone_clean[2:]

    deadline = time.time() + timeout_s

    # 等短信下行延迟
    time.sleep(30)

    while time.time() < deadline:
        raw = _call(token, {"code": "queryUsed"}, timeout=30)
        if raw.startswith("ERROR:"):
            # 频率限制或网络错误，等一个完整轮次再试
            time.sleep(poll_interval)
            continue

        # 逐行解析 tab 分隔格式：手机号\t价格\t短信内容
        for line in raw.splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            rec_phone = re.sub(r"\D", "", parts[0].strip())
            content = parts[2].strip()
            if (rec_phone.endswith(phone_clean) or phone_clean.endswith(rec_phone)) \
                    and KEYWORD in content:
                m = re.search(r"\b(\d{6})\b", content)
                if m:
                    return {"ok": True, "code": m.group(1), "raw": content}

        time.sleep(poll_interval)

    return {"ok": False, "error": f"等待 {timeout_s}s 未收到验证码"}


def release(token: str, phone: str) -> str:
    """释放号码"""
    return _call(token, {"code": "release", "phone": phone})


def block(token: str, phone: str) -> str:
    """拉黑号码（注册失败时拉黑，下次不会再取到）"""
    return _call(token, {"code": "block", "phone": phone})
