"""上游模型列表同步
====================
参照 GitHub 上 `Sliverkiss/workbuddy2api`（Go 版）的 handler.go / client.go 策略实现。

上游**没有** OpenAI 风格的 `/v1/models`（21 条 models 路径实测全 404），
真正的模型清单在 CodeBuddy console 接口：

    GET https://copilot.tencent.com/console/enterprises/personal/models
    -> {"code":0,"data":{
          "models":  [...23 项...],   # 全量表，含 disabled/maxInputTokens/credits
          "agents":  [...13 项...],   # 其中 name=="cli" 那项的 models 才是 CLI 可用的
          "endpoint": "https://copilot.tencent.com", ...
       }}

实测（2026-08-15，池内 10/10 账号 HTTP 200，CLI 可用 11/11）：
  auto / hy3 / glm-5.2 / glm-5.1 / glm-5v-turbo / kimi-k3-1 / kimi-k2.7 /
  kimi-k2.6 / minimax-m3 / deepseek-v4-flash / deepseek-v4-pro

三层策略（与 Go 版一致）：
  1. 正缓存 TTL 1h            —— 命中直接返回，不打上游
  2. 失败进 5min 负缓存        —— 冷却期内不再重试，避免反复打上游/触发风控
  3. STATIC_MODELS 静态兜底    —— 动态彻底不可用时 /v1/models 也不会返回空列表

注意：**不要**再用"逐个模型发 chat 探测"的老办法。上游对短 prompt 批量探测会回
11140 request illegal，结果全 false，而且白烧积分。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import upstream

# 正缓存有效期（小时）/ 失败负缓存冷却（秒）
CACHE_TTL_HOURS = 1.0
FAIL_COOLDOWN = 300

# 静态兜底表：动态接口不可用时用。实测于 2026-08-15。
STATIC_MODELS: list[dict[str, Any]] = [
    {"id": "auto",              "name": "Auto",              "ctx": 168000,  "max_output_tokens": 32000},
    {"id": "hy3",               "name": "Hy3",               "ctx": 192000,  "max_output_tokens": 64000},
    {"id": "glm-5.2",           "name": "GLM-5.2",           "ctx": 1000000, "max_output_tokens": 48000},
    {"id": "glm-5.1",           "name": "GLM-5.1",           "ctx": 200000,  "max_output_tokens": 48000},
    {"id": "glm-5v-turbo",      "name": "GLM-5v-Turbo",      "ctx": 200000,  "max_output_tokens": 64000},
    {"id": "kimi-k3-1",         "name": "Kimi-K3",           "ctx": 1000000, "max_output_tokens": 32000},
    {"id": "kimi-k2.7",         "name": "Kimi-K2.7-Code",    "ctx": 256000,  "max_output_tokens": 32000},
    {"id": "kimi-k2.6",         "name": "Kimi-K2.6",         "ctx": 256000,  "max_output_tokens": 32000},
    {"id": "minimax-m3",        "name": "MiniMax-M3",        "ctx": 512000,  "max_output_tokens": 128000},
    {"id": "deepseek-v4-flash", "name": "Deepseek-V4-Flash", "ctx": 1000000, "max_output_tokens": 50000},
    {"id": "deepseek-v4-pro",   "name": "Deepseek-V4-Pro",   "ctx": 1000000, "max_output_tokens": 50000},
]

# 上游 vendor 字段是混淆过的单字母（"e"/"f"/"j"/"v"），按 id 前缀给人类可读厂商名
_VENDOR_BY_PREFIX = [
    ("glm-",      "Zhipu"),
    ("kimi-",     "Moonshot"),
    ("deepseek-", "DeepSeek"),
    ("minimax-",  "MiniMax"),
    ("hy3",       "Tencent"),
    ("hunyuan",   "Tencent"),
    ("auto",      "Auto"),
    ("default",   "Auto"),
]


def vendor_of(model_id: str) -> str:
    for pre, name in _VENDOR_BY_PREFIX:
        if model_id.startswith(pre):
            return name
    return "codebuddy"


# --------------------------------------------------------------------------- #
# console 未列但实测可调的"漏网"模型
# --------------------------------------------------------------------------- #
# console 接口（含全量 23 项表）里**都没有**这几个 id，但直接 chat 能通。
# 实测 2026-08-15，对照实验排除了"碰巧"与"报错模式相同"的误判：
#   glm-5.3    同一 active 账号连续 3/3 成功；两个 active 账号各 1/1 成功
#   kimi-k3    / kimi-k3-2  均成功出内容（首轮 max_tokens=8 判不出，是因为思考
#              模型把输出全放 reasoning_content，content 为空 —— 不是不可用）
#   对照组 glm-4.7 / glm-4.6（在全量 23 项表里但不在 cli 白名单）与编造的
#              glm-9.9-fake 一律 400 code=11102 "service info not found"
#   disabled 账号打已知可用的 glm-5.2 也是 403 code=11140，证明跨账号失败是
#              账号被禁所致，与模型无关
#
# ctx / max_output_tokens 取**平台同门模型的实际上限**，不取 models.dev 的原生上限：
# 平台会自己压低 output（glm-5.2 原生 131072 → 平台 48000；kimi-k3-1 → 32000），
# 照原生值发会让客户端要到超限的 max_tokens。
UNLISTED_MODELS: list[dict[str, Any]] = [
    {"id": "glm-5.3", "name": "GLM-5.3", "ctx": 1000000, "max_output_tokens": 48000,
     "desc": "官方 console 未列，实测可调。GLM 旗舰（2026-08-14 发布），长程编码/Agent 向",
     "supports_images": False, "supports_tool_call": True, "supports_reasoning": True,
     "credits": None, "unlisted": True},
    {"id": "kimi-k3", "name": "Kimi-K3", "ctx": 1000000, "max_output_tokens": 32000,
     "desc": "官方 console 未列，实测可调。Kimi 当前最新代（console 只给了 kimi-k3-1 快照）",
     "supports_images": True, "supports_tool_call": True, "supports_reasoning": True,
     "credits": None, "unlisted": True},
    {"id": "kimi-k3-2", "name": "Kimi-K3-2", "ctx": 1000000, "max_output_tokens": 32000,
     "desc": "官方 console 未列，实测可调。kimi-k3 的另一个平台快照",
     "supports_images": True, "supports_tool_call": True, "supports_reasoning": True,
     "credits": None, "unlisted": True},
]


def merge_unlisted(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把漏网模型并进清单（已存在的 id 不覆盖）。"""
    have = {m.get("id") for m in models}
    return list(models) + [dict(m, vendor_label=vendor_of(m["id"]))
                           for m in UNLISTED_MODELS if m["id"] not in have]


def static_models() -> list[dict[str, Any]]:
    """静态兜底全量（console 11 项 + 漏网 3 项）。"""
    return merge_unlisted([dict(m, vendor_label=vendor_of(m["id"])) for m in STATIC_MODELS])


# --------------------------------------------------------------------------- #
# 缓存读写
# --------------------------------------------------------------------------- #
def _empty() -> dict[str, Any]:
    return {"models": [], "timestamp": 0, "available_count": 0,
            "source": None, "last_fail": 0, "last_error": None}


def load_models_cache(cache_path: Path) -> dict[str, Any]:
    """读缓存。**始终返回 dict**（models 字段是 list[dict]）。"""
    if not cache_path.exists():
        return _empty()
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    models = data.get("models")
    if not isinstance(models, list):
        data["models"] = []
    # 兼容老格式：models 曾经是 list[str]
    elif models and isinstance(models[0], str):
        data["models"] = [{"id": m, "name": m, "ctx": 128000,
                           "max_output_tokens": 4096} for m in models]
    return _empty() | data


def save_models_cache(cache_path: Path, data: dict[str, Any]) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(cache_path)
    except Exception:  # noqa: BLE001
        pass


def is_cache_expired(cache_path: Path, max_age_hours: float = CACHE_TTL_HOURS) -> bool:
    cache = load_models_cache(cache_path)
    if not cache["models"]:
        return True
    return (time.time() - cache.get("timestamp", 0)) / 3600 > max_age_hours


def in_fail_cooldown(cache: dict[str, Any]) -> bool:
    """上次拉取失败后 5min 内不再打上游（Go 版 modelsFetchFailCooldown）。"""
    lf = cache.get("last_fail") or 0
    return bool(lf) and (time.time() - lf) < FAIL_COOLDOWN


# --------------------------------------------------------------------------- #
# 同步
# --------------------------------------------------------------------------- #
def sync_models_from_upstream(token: str, proxy: str | None = None,
                              cache_path: Path | None = None) -> dict[str, Any]:
    """
    调官方 console 接口刷新模型列表并落缓存。

    成功 -> {"ok": True,  "available_count": N, "models": [...], "synced_at": ...}
    失败 -> {"ok": False, "error": "...", 保留上一次的 models}
    """
    prev = load_models_cache(cache_path) if cache_path else _empty()
    res = upstream.fetch_official_models(token, proxy=proxy)

    if res.get("error") or not res.get("models"):
        out = prev | {
            "ok": False,
            "error": res.get("error") or "上游返回空模型列表",
            "last_fail": time.time(),
            "last_error": res.get("error"),
        }
        if cache_path:
            save_models_cache(cache_path, out)
        return out

    models = merge_unlisted(res["models"])
    for m in models:
        m.setdefault("vendor_label", vendor_of(m["id"]))
    out = {
        "ok": True,
        "models": models,
        "available_count": len(models),
        "timestamp": time.time(),
        "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "console_api",
        "last_fail": 0,
        "last_error": None,
        "error": None,
    }
    if cache_path:
        save_models_cache(cache_path, out)
    return out


def resolve_models(cache_path: Path) -> tuple[list[dict[str, Any]], str]:
    """
    只读解析：缓存有 -> 缓存；否则 -> 静态兜底表。
    返回 (models, source)，source ∈ {"console_api", "cache", "static"}
    """
    cache = load_models_cache(cache_path)
    if cache["models"]:
        # 老缓存（本次改动之前落的）没有漏网模型，读的时候补上
        return merge_unlisted(cache["models"]), cache.get("source") or "cache"
    return static_models(), "static"


def to_openai_data(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """转 OpenAI /v1/models 的 data 数组（带 context_length，兼容各类客户端）。"""
    now = int(time.time())
    out: list[dict[str, Any]] = []
    for m in models:
        mid = m.get("id")
        if not mid:
            continue
        entry = {
            "id": mid,
            "object": "model",
            "created": now,
            "owned_by": m.get("vendor_label") or vendor_of(mid),
            "context_length": m.get("ctx") or 131072,
            "max_output_tokens": m.get("max_output_tokens") or 4096,
            "display_name": m.get("name") or mid,
        }
        for k in ("credits", "desc", "supports_images", "supports_tool_call",
                  "supports_reasoning", "is_default", "unlisted"):
            if m.get(k) is not None:
                entry[k] = m[k]
        out.append(entry)
    return out
