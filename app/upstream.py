"""
WorkBuddy / CodeBuddy 上游 API 封装
===================================
所有端点均为 2026-08-13 实测确认：
  - POST /v2/chat/completions           仅支持 stream=true（stream=false → 11101）
  - POST /v2/billing/meter/get-user-resource   余额
  - POST /v2/billing/meter/daily-checkin       每日签到 +100
  - POST /v2/plugin/auth/state?platform=workbuddy   免 token，用作代理探针
  - 无模型列表接口（21 条 models 路径全 404）→ 只能候选名单并发探测
"""
from __future__ import annotations

import base64
import json
import random
import re
import time
from typing import Any, Iterator

import httpx

COPILOT = "https://copilot.tencent.com"
CONSOLE = "https://www.codebuddy.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 用于注册时伪装不同浏览器/设备，避免每次请求特征完全一致
_WIN_VERS  = ["10.0", "11.0"]
_MAC_VERS  = ["10_15_7", "14_4_1", "13_6_4"]
_CHROME    = [131, 132, 133, 134, 135]
_SAFARI_VER = "537.36"

def random_ua() -> str:
    """每次注册生成一个不同的 Chrome UA（Windows / macOS 随机）。"""
    chrome = random.choice(_CHROME)
    if random.random() < 0.5:                  # Windows
        win = random.choice(_WIN_VERS)
        return (f"Mozilla/5.0 (Windows NT {win}; Win64; x64) "
                f"AppleWebKit/{_SAFARI_VER} (KHTML, like Gecko) "
                f"Chrome/{chrome}.0.0.0 Safari/{_SAFARI_VER}")
    else:                                       # macOS
        mac = random.choice(_MAC_VERS)
        return (f"Mozilla/5.0 (Macintosh; Intel Mac OS X {mac}) "
                f"AppleWebKit/{_SAFARI_VER} (KHTML, like Gecko) "
                f"Chrome/{chrome}.0.0.0 Safari/{_SAFARI_VER}")

def random_accept_language() -> str:
    choices = [
        "zh-CN,zh;q=0.9,en;q=0.8",
        "zh-CN,zh;q=0.9",
        "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "zh-CN,zh;q=0.9,en-US;q=0.8",
        "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    ]
    return random.choice(choices)

def random_screen() -> dict[str, int]:
    """常见显示器分辨率（注册请求 headers 里可选填）。"""
    screens = [(1920, 1080), (2560, 1440), (1366, 768),
               (1440, 900), (1280, 800), (1680, 1050)]
    w, h = random.choice(screens)
    return {"width": w, "height": h}

# 候选模型名单：探测用。命名不能靠直觉猜（hunyuan-2.0 不存在但 hunyuan-2.0-instruct 在）
MODEL_CANDIDATES = [
    "kimi-k3", "kimi-k2.6", "kimi-k2.5",
    "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v3", "deepseek-v3-2-volc", "deepseek-r1",
    "hunyuan-2.0-instruct",
    "glm-5.3", "glm-5.2", "glm-5.1", "glm-5.0",
    "minimax-m2.7",
    "default", "auto",
    # 以下历史上出现过/可能开放，保留探测
    "kimi-k2", "deepseek-v4", "hunyuan-3.0", "hunyuan-turbos", "glm-4.6",
    "minimax-m2.8", "qwen3-max", "qwen3-coder", "gpt-5.1", "gemini-2.5-pro",
    "claude-sonnet-4-6", "claude-opus-4-8",
]

# 已实测的相对倍率（credits/1k tokens 量级参考，仅供 UI 展示，真实计费以上游为准）
# 上游计费异步，精确倍率由代理侧记账 + 余额差归因得出，见 accounting.py
MODEL_META: dict[str, dict[str, Any]] = {
    "kimi-k3":              {"vendor": "Moonshot",  "tier": "flagship", "ctx": 256000},
    "kimi-k2.6":            {"vendor": "Moonshot",  "tier": "standard", "ctx": 256000},
    "kimi-k2.5":            {"vendor": "Moonshot",  "tier": "standard", "ctx": 256000},
    "deepseek-v4-pro":      {"vendor": "DeepSeek",  "tier": "flagship", "ctx": 128000},
    "deepseek-v4-flash":    {"vendor": "DeepSeek",  "tier": "fast",     "ctx": 128000},
    "deepseek-v3":          {"vendor": "DeepSeek",  "tier": "standard", "ctx": 128000},
    "deepseek-v3-2-volc":   {"vendor": "DeepSeek",  "tier": "standard", "ctx": 128000},
    "deepseek-r1":          {"vendor": "DeepSeek",  "tier": "reasoning","ctx": 64000},
    "hunyuan-2.0-instruct": {"vendor": "Tencent",   "tier": "standard", "ctx": 32000},
    "glm-5.3":              {"vendor": "Zhipu",     "tier": "flagship", "ctx": 200000},
    "glm-5.2":              {"vendor": "Zhipu",     "tier": "flagship", "ctx": 200000},
    "glm-5.1":              {"vendor": "Zhipu",     "tier": "standard", "ctx": 200000},
    "glm-5.0":              {"vendor": "Zhipu",     "tier": "standard", "ctx": 128000},
    "minimax-m2.7":         {"vendor": "MiniMax",   "tier": "reasoning","ctx": 200000},
    "default":              {"vendor": "Auto",      "tier": "alias",    "ctx": 128000},
    "auto":                 {"vendor": "Auto",      "tier": "alias",    "ctx": 128000},
}


def auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Accept": "*/*",
    }


def decode_jwt(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# 代理探针（免 token）
# --------------------------------------------------------------------------- #
def probe_proxy(proxy: str | None, timeout: float = 25.0) -> tuple[bool, str]:
    """打真实业务端点判断出口可用。通用探针(ipify)绿灯 ≠ 目标可用。"""
    try:
        with httpx.Client(proxy=proxy, timeout=timeout, verify=True) as c:
            r = c.post(f"{COPILOT}/v2/plugin/auth/state?platform=workbuddy",
                       json={}, headers={"User-Agent": UA, "Content-Type": "application/json"})
        if r.status_code == 200 and r.json().get("code") == 0:
            return True, "ok"
        return False, f"http {r.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:120]


# --------------------------------------------------------------------------- #
# 余额 / 签到
# --------------------------------------------------------------------------- #
def get_balance(token: str, proxy: str | None = None,
                retries: int = 3, timeout: float = 30.0) -> dict[str, Any]:
    """
    返回 {"total": float, "packages": [...], "raw_total_count": int}
    注册后立刻查会拿到 -1/空（套餐异步发放），故内建重试 + sleep。
    """
    body = {
        "PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca", "Status": [0, 3],
        "PackageStartTimeRangeBegin": "2024-12-01 21:25:00",
        "PackageStartTimeRangeEnd": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    last = ""
    for attempt in range(retries):
        try:
            with httpx.Client(proxy=proxy, timeout=timeout) as c:
                r = c.post(f"{COPILOT}/v2/billing/meter/get-user-resource",
                           json=body, headers=auth_headers(token))
            j = r.json()
            data = (j.get("data") or {}).get("Response", {}).get("Data", {}) or {}
            accs = data.get("Accounts") or []
            if accs:
                pkgs = [{
                    "name": a.get("PackageName"),
                    "remain": float(a.get("CycleCapacityRemainPrecise") or 0),
                    "capacity": float(a.get("CycleCapacity") or 0),
                    "used": float(a.get("CapacityUsed") or 0),
                    "cycle_end": a.get("CycleEndTime"),
                    "unit": a.get("CapacityUnit") or "credits",
                    "cycle_start": a.get("CycleStartTime"),
                    "size": float(a.get("CycleCapacitySizePrecise") or 0),
                    "status": a.get("Status"),
                } for a in accs]
                # 账号在腾讯侧的真实注册时间。
                # 上游没有 user/info 之类端点（实测 16 条路径全 404），
                # JWT 里也只有本次登录的 iat/auth_time。唯一可靠来源是
                # 「体验版」套餐的 CreateTime —— 注册即发放，一个号只有一份。
                # 兜底取所有套餐里最早的 CreateTime。
                created_ms = 0
                for a in accs:
                    ct = int(a.get("CreateTime") or 0)
                    if not ct:
                        continue
                    if "体验版" in str(a.get("PackageName") or ""):
                        created_ms = ct if not created_ms else min(created_ms, ct)
                if not created_ms:
                    cts = [int(a.get("CreateTime") or 0) for a in accs]
                    cts = [c for c in cts if c > 0]
                    created_ms = min(cts) if cts else 0
                # 今日签到奖励实际到账额。上游把签到积分发成
                # 「CodeBuddy个人版国内运营裂变包」（SubProductName=赠送包），
                # 发放时刻 00:03~00:21 早于本地签到 cron，于是 daily_checkin
                # 只回 10001「今天已签到」、credit=0，面板上看不到任何数字。
                # 所以「今天签到给了多少」必须从包体反推：今天新发的裂变包面额之和。
                _today = time.strftime("%Y-%m-%d")
                _grant, _grant_at = 0.0, ""
                for p in pkgs:
                    cs = str(p.get("cycle_start") or "")
                    if not cs.startswith(_today):
                        continue
                    if "裂变包" not in str(p.get("name") or ""):
                        continue
                    _grant += p["size"]
                    if not _grant_at or cs < _grant_at:
                        _grant_at = cs
                return {
                    "total": round(sum(p["remain"] for p in pkgs), 4),
                    "daily_grant": round(_grant, 4),
                    "daily_grant_at": _grant_at,
                    "packages": pkgs,
                    "raw_total_count": data.get("TotalCount"),
                    "total_dosage": data.get("TotalDosage"),
                    "registered_ms": created_ms,
                    "registered_at": (
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created_ms / 1000))
                        if created_ms else ""
                    ),
                }
            last = f"empty accounts (code={j.get('code')} msg={j.get('msg')})"
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:160]
        if attempt < retries - 1:
            time.sleep(3)
    return {"total": -1.0, "packages": [], "error": last}


def daily_checkin(token: str, proxy: str | None = None, timeout: float = 30.0) -> dict[str, Any]:
    try:
        with httpx.Client(proxy=proxy, timeout=timeout) as c:
            r = c.post(f"{COPILOT}/v2/billing/meter/daily-checkin",
                       json={}, headers=auth_headers(token))
        j = r.json()
        if j.get("code") == 0:
            d = j.get("data") or {}
            return {"ok": True, "credit": d.get("credit", 0),
                    "streak_days": d.get("streak_days"), "raw": d}
        msg = str(j.get("msg") or "")
        # 上游 10001 = 今天已签到。这是"已完成"不是失败，
        # 否则 last_checkin 永远写不进去，每天会重复白打上游。
        if j.get("code") == 10001 or "已签到" in msg:
            return {"ok": False, "already": True, "credit": 0,
                    "error": f"code={j.get('code')} msg={msg}"}
        return {"ok": False, "error": f"code={j.get('code')} msg={msg}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160]}


# --------------------------------------------------------------------------- #
# Chat —— 上游只支持 stream=true
# --------------------------------------------------------------------------- #
class UpstreamError(Exception):
    def __init__(self, status: int, code: Any, msg: str, body: str = ""):
        super().__init__(f"upstream {status} code={code}: {msg}")
        self.status = status
        self.code = code
        self.msg = msg
        self.body = body


def stream_chat(token: str, payload: dict[str, Any], proxy: str | None = None,
                timeout: float = 300.0) -> Iterator[dict[str, Any]]:
    """
    向上游发流式请求，逐块 yield 解析后的 JSON chunk。
    强制 stream=true —— 上游 stream=false 返回 400/11101。
    """
    body = dict(payload)
    body["stream"] = True
    body.pop("stream_options", None)

    with httpx.Client(proxy=proxy, timeout=httpx.Timeout(timeout, connect=30.0)) as client:
        with client.stream("POST", f"{COPILOT}/v2/chat/completions",
                           json=body, headers=auth_headers(token)) as resp:
            if resp.status_code != 200:
                raw = resp.read().decode("utf-8", "replace")
                code, msg = None, raw[:300]
                try:
                    j = json.loads(raw)
                    # 上游错误码有两种摆法：顶层 {"code":...} 和嵌套
                    # {"error":{"data":{"code":...}}}。只取顶层会拿到 None，
                    # 于是 main.py 拼出的 last_error 变成 "None: {整个JSON}" ——
                    # 整个 JSON 体进了待分类字符串，14018 里的 "401" 子串
                    # 就会被 AUTH_KEYWORDS 命中，把额度用尽误判成鉴权失败。
                    inner = ((j.get("error") or {}).get("data") or {}) \
                        if isinstance(j.get("error"), dict) else {}
                    code = j.get("code")
                    if code is None:
                        code = inner.get("code")
                    msg = str(j.get("msg") or j.get("error_msg")
                              or inner.get("msg") or raw)[:300]
                except Exception:  # noqa: BLE001
                    pass
                raise UpstreamError(resp.status_code, code, msg, raw[:600])

            for line in resp.iter_lines():
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "replace")
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue


def probe_model(token: str, model: str, proxy: str | None = None) -> dict[str, Any]:
    """探测单个模型是否可用。200+SSE = 可用；400/11102 = 不存在或未授权。"""
    try:
        text, echoed = "", None
        for chunk in stream_chat(token, {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
        }, proxy=proxy, timeout=60.0):
            echoed = echoed or chunk.get("model")
            for ch in chunk.get("choices") or []:
                text += (ch.get("delta") or {}).get("content") or ""
            if len(text) > 8:
                break
        return {"model": model, "available": True, "echoed": echoed or model,
                "sample": text[:40]}
    except UpstreamError as exc:
        return {"model": model, "available": False,
                "reason": f"{exc.code}: {exc.msg}"[:150]}
    except Exception as exc:  # noqa: BLE001
        return {"model": model, "available": False, "reason": str(exc)[:150]}


# --------------------------------------------------------------------------- #
# Token 刷新（Keycloak）
# --------------------------------------------------------------------------- #
def refresh_token(refresh_tok: str, proxy: str | None = None,
                  timeout: float = 30.0) -> dict[str, Any]:
    """
    Keycloak refresh_token grant。client_id=console。
    返回 {"access_token","refresh_token","expires_at"} 或 {"error": ...}
    """
    try:
        with httpx.Client(proxy=proxy, timeout=timeout, follow_redirects=True) as c:
            r = c.post(
                f"{CONSOLE}/auth/realms/copilot/protocol/openid-connect/token",
                data={"grant_type": "refresh_token", "refresh_token": refresh_tok,
                      "client_id": "console"},
                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA},
            )
        if r.status_code != 200:
            return {"error": f"http {r.status_code}: {r.text[:200]}"}
        j = r.json()
        at = j.get("access_token")
        if not at:
            return {"error": f"no access_token: {r.text[:200]}"}
        dec = decode_jwt(at)
        return {
            "access_token": at,
            "refresh_token": j.get("refresh_token") or refresh_tok,
            "expires_at": int(dec.get("exp", 0)) * 1000,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


def extract_sms_code(sms: str, phone: str = "") -> str | None:
    """
    从短信正文提取验证码。
    UoomMsg 返回格式是「号码/单价/正文」三段，直接 \\d{6} 会抓到手机号前 6 位。
    """
    text = (sms or "").strip()
    parts = text.split("/", 2)
    if len(parts) == 3 and parts[0].isdigit():
        text = parts[2]
    if phone:
        text = text.replace(phone.lstrip("+").replace("86", "", 1), " ")
        text = text.replace(phone.lstrip("+"), " ").replace(phone, " ")
    for pat in (r"【[^】]*】\s*(\d{4,8})",
                r"(\d{4,8})\s*为您的",
                r"验证码[^\d]{0,8}(\d{4,8})",
                r"(?<!\d)(\d{6})(?!\d)"):
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


def fetch_official_models(token: str, proxy: str | None = None,
                          timeout: float = 30.0) -> dict[str, Any]:
    """
    从官方 console 接口获取本账号真实可用的模型列表。

    实测（2026-08-15，10/10 账号 HTTP 200）：
      GET {COPILOT}/console/enterprises/personal/models
      -> {"code":0,"data":{"models":[...23 项...],"agents":[...13 项...],"endpoint":...}}

    坑：
      - agents 每项用 **name** 标识（没有 key 字段），CLI 那项是 name=="cli"
      - data.models 是全量表（23 个），要用 agents["cli"].models 这 11 个 id 过滤
      - 倍率在 credits 字段里，形如 "x0.79 credits"
    返回 {"models": [...], "error": None}
    """
    try:
        with httpx.Client(proxy=proxy, timeout=timeout, follow_redirects=True) as c:
            r = c.get(f"{COPILOT}/console/enterprises/personal/models",
                      headers=auth_headers(token) | {
                          "Accept": "application/json",
                          "Origin": CONSOLE,
                          "Referer": f"{CONSOLE}/",
                      })
        if r.status_code != 200:
            return {"models": [], "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        payload = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"models": [], "error": f"{type(exc).__name__}: {exc}"[:200]}

    if payload.get("code") != 0:
        return {"models": [], "error": f"code={payload.get('code')} {str(payload.get('msg'))[:120]}"}

    data = payload.get("data") or {}
    cli_ids: list[str] = []
    for ag in data.get("agents") or []:
        if ag.get("name") == "cli":
            cli_ids = list(ag.get("models") or [])
            break
    if not cli_ids:
        return {"models": [], "error": "agents 里没有 cli 项"}

    by_id = {m.get("id"): m for m in (data.get("models") or []) if m.get("id")}
    out: list[dict[str, Any]] = []
    for mid in cli_ids:
        m = by_id.get(mid)
        if not m or m.get("disabled"):
            continue
        out.append({
            "id": mid,
            "name": m.get("name") or mid,
            "vendor": m.get("vendor") or "codebuddy",
            "ctx": m.get("maxInputTokens") or 128000,
            "max_output_tokens": m.get("maxOutputTokens") or 4096,
            "credits": m.get("credits"),
            "desc": m.get("descriptionZh") or m.get("descriptionEn"),
            "supports_images": bool(m.get("supportsImages")),
            "supports_tool_call": bool(m.get("supportsToolCall")),
            "supports_reasoning": bool(m.get("supportsReasoning")),
            "is_default": bool(m.get("isDefault")),
        })
    if not out:
        return {"models": [], "error": "cli 模型全部 disabled"}
    return {"models": out, "error": None}
