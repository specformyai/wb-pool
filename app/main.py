"""
wb-pool —— WorkBuddy/CodeBuddy 账号池反向代理
=============================================
对外暴露：
  OpenAI 兼容：GET /v1/models, POST /v1/chat/completions（stream 与非 stream 均支持）
  Anthropic 兼容：POST /v1/messages
  管理 API：/api/*
  WebUI：/

上游只支持 stream=true，非流式请求由本层聚合后返回完整 JSON。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import upstream
from . import invite as invite_mod
from . import uoomsg as uum
from .accounting import Ledger
from .apikeys import KeyStore
from .calllog import CallLog
from .pool import Account, AccountPool
from .proxies import DEFAULT_EXITS, ProxyManager, is_proxy_error
from .register import Registrar
from .auto_register import AutoRegistrar
from .upstream_sync import (STATIC_MODELS, static_models, merge_unlisted,
                            is_cache_expired, in_fail_cooldown,
                            load_models_cache as load_sync_cache, resolve_models,
                            sync_models_from_upstream, to_openai_data, vendor_of)
from .webauth import COOKIE_NAME, SESSION_TTL, WebAuth


class _EmptyUpstreamStream(Exception):
    """Sentinel exception used when advancing a sync generator in a worker thread."""


def _next_upstream_chunk(gen: Iterator[Any]) -> Any:
    try:
        return next(gen)
    except StopIteration as exc:
        raise _EmptyUpstreamStream from exc

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("WB_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

ACCOUNTS_FILE = Path(os.environ.get("WB_ACCOUNTS_FILE", DATA_DIR / "accounts.jsonl"))
MODELS_CACHE = DATA_DIR / "models_cache.json"
PROXY_STATE = DATA_DIR / "proxy_state.json"
APIKEYS_FILE = DATA_DIR / "apikeys.json"
WEBAUTH_FILE = DATA_DIR / "webauth.json"
CALLS_FILE = DATA_DIR / "calls.jsonl"

API_KEY = os.environ.get("WB_API_KEY", "")            # 兼容老客户端的内建 key
ADMIN_KEY = os.environ.get("WB_ADMIN_KEY", API_KEY)   # 管理接口后备密钥（供脚本用）
PROXY_MODE = os.environ.get("WB_PROXY_MODE", "off")   # off | fixed | rotate
PROXY_HOST = os.environ.get("WB_PROXY_HOST", "127.0.0.1")
PROXY_FIXED = os.environ.get("WB_PROXY_URL", "")
CHECKIN_CRON = os.environ.get("WB_CHECKIN_CRON", "5 1 * * *")
BALANCE_INTERVAL = int(os.environ.get("WB_BALANCE_INTERVAL_MIN", "30"))
UOOMSG_TOKEN = os.environ.get("WB_UOOMSG_TOKEN", "")
COOKIE_SECURE = os.environ.get("WB_COOKIE_SECURE", "auto")   # auto | on | off

pool = AccountPool(ACCOUNTS_FILE)
ledger = Ledger(DATA_DIR / "ledger.json")
keystore = KeyStore(APIKEYS_FILE, env_key=API_KEY)
webauth = WebAuth(WEBAUTH_FILE)
calllog = CallLog(CALLS_FILE)
pm = ProxyManager(mode=PROXY_MODE, host=PROXY_HOST, fixed_url=PROXY_FIXED,
                  exits=DEFAULT_EXITS, state_file=PROXY_STATE)
registrar = Registrar(pool, pm)
auto_registrar = AutoRegistrar(registrar, UOOMSG_TOKEN)
# 出口故障时账号池要能自己换线重试
pool.proxy_mgr = pm
scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

app = FastAPI(title="wb-pool", version="1.1.0", docs_url="/api/docs", redoc_url=None)


# --------------------------------------------------------------------------- #
# 鉴权
#   /v1/*  —— API key（keystore 里的任意一把，或 .env 的 WB_API_KEY）
#   /api/* —— WebUI session cookie；也接受 ADMIN_KEY 便于脚本直连
# --------------------------------------------------------------------------- #
def _extract_key(authorization: str | None, x_api_key: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return (x_api_key or "").strip()


def require_api(request: Request,
                authorization: str | None = Header(None),
                x_api_key: str | None = Header(None, alias="x-api-key")) -> dict[str, Any]:
    """校验反代 key，并把命中的 key 记录挂到 request.state 供落账使用。"""
    rec = keystore.verify(_extract_key(authorization, x_api_key))
    if rec is None:
        raise HTTPException(401, "invalid api key")
    request.state.wb_key = rec
    return rec


def require_admin(authorization: str | None = Header(None),
                  x_api_key: str | None = Header(None, alias="x-api-key"),
                  wb_session: str | None = Cookie(None, alias=COOKIE_NAME)) -> str:
    """WebUI/管理接口：优先 session cookie，其次 ADMIN_KEY（脚本/curl）。"""
    s = webauth.session(wb_session or "")
    if s:
        return s["user"]
    if ADMIN_KEY and _extract_key(authorization, x_api_key) == ADMIN_KEY:
        return "admin-key"
    raise HTTPException(401, "未登录")


def _key_of(request: Request) -> dict[str, Any]:
    return getattr(request.state, "wb_key", None) or {"id": "", "name": ""}


def _log_call(request: Request, *, model: str, ok: bool, endpoint: str,
              t0: float, tokens: int = 0, credits: float = 0.0,
              account: str = "", code: Any = None, error: str = "",
              stream: bool = False, t_first: float = 0.0,
              out_tokens: int = 0) -> None:
    """一次上游调用落一行 calls.jsonl，并把用量记到对应的 API key 上。

    t_first  = 收到上游首包的时刻（用来算首字延迟）
    out_tokens = 输出 token 数（用来算 t/s，只统计首包之后的生成阶段）
    """
    k = _key_of(request)
    now = time.time()
    ttft = int((t_first - t0) * 1000) if t_first > t0 else 0
    gen_s = (now - t_first) if t_first > t0 else 0.0
    tps = (out_tokens / gen_s) if (out_tokens > 0 and gen_s > 0.05) else 0.0
    try:
        calllog.record(model=model, ok=ok, endpoint=endpoint,
                       ms=int((now - t0) * 1000), ttft_ms=ttft, tps=tps,
                       tokens=tokens, credits=credits, account=account,
                       key_id=k.get("id", ""), key_name=k.get("name", ""),
                       code=code, error=error, stream=stream)
    except Exception:  # noqa: BLE001
        pass
    if ok:
        keystore.record_use(k.get("id", ""), tokens=tokens, credits=credits)


# --------------------------------------------------------------------------- #
# 模型缓存
# --------------------------------------------------------------------------- #
def probe_models(force: bool = False) -> dict[str, Any]:
    """
    取模型清单。参照 workbuddy2api(Go) 的 fetchDynamicModels：
      正缓存 1h -> 失败 5min 负缓存 -> 静态兜底，绝不逐个 chat 探测（会 11140 且烧积分）。
    """
    cache = load_sync_cache(MODELS_CACHE)
    if not force and cache["models"] and not is_cache_expired(MODELS_CACHE):
        # 缓存是 console 返回的 11 项，漏网模型（glm-5.3 等）要在读的时候并进来
        return {"models": merge_unlisted(cache["models"]), "source": cache.get("source") or "cache",
                "probed_at": cache.get("timestamp"), "error": None}
    if not force and in_fail_cooldown(cache):
        models, source = resolve_models(MODELS_CACHE)
        return {"models": models, "source": source, "probed_at": cache.get("timestamp"),
                "error": f"上游拉取失败冷却中: {cache.get('last_error')}"}

    acc = pool.acquire(proxy=pm.pick())
    if not acc:
        models, source = resolve_models(MODELS_CACHE)
        return {"models": models, "source": source,
                "probed_at": cache.get("timestamp"), "error": "池中无可用账号"}
    try:
        res = sync_models_from_upstream(token=acc.access_token, proxy=pm.pick(),
                                       cache_path=MODELS_CACHE)
    finally:
        pool.release(acc)

    if res.get("ok"):
        return {"models": res["models"], "source": "console_api",
                "probed_at": res.get("timestamp"), "error": None}
    models, source = resolve_models(MODELS_CACHE)
    return {"models": models, "source": source,
            "probed_at": cache.get("timestamp"), "error": res.get("error")}


# --------------------------------------------------------------------------- #
# OpenAI 兼容
# --------------------------------------------------------------------------- #
@app.get("/v1/models", dependencies=[Depends(require_api)])
def list_models() -> dict[str, Any]:
    """OpenAI 兼容模型列表：缓存 -> 静态兜底，永不返回空数组。"""
    models, _source = resolve_models(MODELS_CACHE)
    return {"object": "list", "data": to_openai_data(models)}


def _sse(obj: Any) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _pick_account(force_key: str | None = None) -> Account:
    if force_key:
        acc, err = pool.acquire_specific(force_key, proxy=pm.pick())
        if not acc:
            raise HTTPException(400, f"指定账号不可用: {err}")
        return acc
    acc = pool.acquire(proxy=pm.pick())
    if not acc:
        raise HTTPException(503, "账号池中没有可用账号，请先在 WebUI 添加账号")
    return acc


@app.post("/api/chat/completions", dependencies=[Depends(require_admin)])
@app.post("/v1/chat/completions", dependencies=[Depends(require_api)])
async def chat_completions(request: Request) -> Any:
    body = await request.json()
    want_stream = bool(body.get("stream"))
    model = body.get("model") or "default"
    payload = {k: v for k, v in body.items() if k not in ("stream", "stream_options")}
    payload["model"] = model

    # WebUI 调试请求带这个头；限制上游无数据等待，避免一个坏连接永久占住账号。
    # 普通 API 调用继续保留原来的 300 秒上限，不擅自缩短外部客户端长回答。
    upstream_timeout = 300.0
    raw_timeout = (request.headers.get("X-WB-Debug-Timeout") or "").strip()
    is_debug_request = bool(raw_timeout)
    if raw_timeout:
        try:
            upstream_timeout = max(10.0, min(180.0, float(raw_timeout)))
        except ValueError:
            upstream_timeout = 120.0

    # X-WB-Force-Account: 手机号或 masked，调试时指定账号
    force_key = (request.headers.get("X-WB-Force-Account") or "").strip() or None

    last_err: str | None = None
    tried: list[str] = []
    # WebUI 调试以“按时释放”为优先，不在客户端停止后继续串行重试多个账号。
    max_tries = 1 if force_key or is_debug_request else min(3, max(1, len(pool.all())))
    for _ in range(max_tries):
        t0 = time.time()
        acc = _pick_account(force_key)
        if acc.masked() in tried:
            continue
        tried.append(acc.masked())
        proxy = pm.pick()
        try:
            gen = upstream.stream_chat(acc.access_token, payload, proxy=proxy,
                                       timeout=upstream_timeout)
            # stream_chat 是同步 httpx 生成器，不能在 async 路由里直接 next，
            # 否则首包卡住时会把整个事件循环（包括其它管理操作）一起卡住。
            first = await asyncio.to_thread(_next_upstream_chunk, gen)
            t_first = time.time()      # 首字延迟锚在这里
        except upstream.UpstreamError as exc:
            last_err = f"{exc.code}: {exc.msg}"
            _log_call(request, model=model, ok=False, endpoint="chat", t0=t0,
                      account=acc.masked(), code=exc.code, error=exc.msg,
                      stream=want_stream)
            if force_key:
                # 指定账号调试也要落状态；否则持续 11140 的封禁号会一直显示 usable。
                pool.release(acc, error=last_err)
                raise HTTPException(502, {"error": {"message": last_err,
                                                    "code": exc.code,
                                                    "type": "upstream_error"}})
            pool.release(acc, error=last_err)
            if exc.code == 11102:
                raise HTTPException(400, {"error": {"message": exc.msg, "code": exc.code,
                                                    "type": "invalid_request_error"}})
            continue
        except _EmptyUpstreamStream:
            last_err = "上游返回空流"
            _log_call(request, model=model, ok=False, endpoint="chat", t0=t0,
                      account=acc.masked(), error=last_err, stream=want_stream)
            if force_key:
                raise HTTPException(502, {"error": {"message": last_err, "type": "upstream_error"}})
            pool.release(acc, error=last_err)
            continue
        except Exception as exc:       # noqa: BLE001
            last_err = str(exc)[:200]
            _log_call(request, model=model, ok=False, endpoint="chat", t0=t0,
                      account=acc.masked(), error=last_err, stream=want_stream)
            if force_key:
                raise HTTPException(502, {"error": {"message": last_err, "type": "upstream_error"}})
            # 代理链路故障：拉黑该出口，换出口重试，不污染账号 last_error
            if is_proxy_error(last_err):
                pm.mark_bad(proxy)
                continue
            pool.release(acc, error=last_err)
            continue

        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        if want_stream:
            def event_stream() -> Iterator[str]:
                usage: dict[str, Any] | None = None
                try:
                    for chunk in _chain(first, gen):
                        usage = chunk.get("usage") or usage
                        yield _sse(_norm_chunk(chunk, cid, created, model))
                    yield "data: [DONE]\n\n"
                    credit = ledger.record(model, usage)
                    tk = (usage or {}).get("total_tokens", 0)
                    pool.release(acc, tokens=tk, credits=credit)
                    _log_call(request, model=model, ok=True, endpoint="chat", t0=t0,
                              tokens=tk, credits=credit, account=acc.masked(), stream=True,
                              t_first=t_first,
                              out_tokens=(usage or {}).get("completion_tokens", 0))
                except Exception as exc:  # noqa: BLE001
                    pool.release(acc, error=str(exc)[:200])
                    _log_call(request, model=model, ok=False, endpoint="chat", t0=t0,
                              account=acc.masked(), error=str(exc)[:200], stream=True)
                    yield _sse({"error": {"message": str(exc)[:200], "type": "upstream_error"}})
                    yield "data: [DONE]\n\n"

            return StreamingResponse(event_stream(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no",
                                              "X-WB-Account": acc.masked()})

        # 非流式：聚合上游流
        content, reasoning, usage, finish, tool_calls = "", "", None, "stop", []
        try:
            content, reasoning, usage, finish, tool_calls = await asyncio.to_thread(
                _collect_chat, first, gen)
        except Exception as exc:  # noqa: BLE001
            pool.release(acc, error=str(exc)[:200])
            _log_call(request, model=model, ok=False, endpoint="chat", t0=t0,
                      account=acc.masked(), error=str(exc)[:200])
            raise HTTPException(502, f"上游流中断: {exc}"[:200])

        credit = ledger.record(model, usage)
        tk = (usage or {}).get("total_tokens", 0)
        pool.release(acc, tokens=tk, credits=credit)
        _log_call(request, model=model, ok=True, endpoint="chat", t0=t0,
                  tokens=tk, credits=credit, account=acc.masked(),
                  t_first=t_first, out_tokens=(usage or {}).get("completion_tokens", 0))
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning:
            msg["reasoning_content"] = reasoning
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return JSONResponse({
            "id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish or "stop"}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }, headers={"X-WB-Account": acc.masked()})

    raise HTTPException(502, f"全部账号均失败，最后错误: {last_err}")


def _chain(first: Any, gen: Iterator[Any]) -> Iterator[Any]:
    yield first
    yield from gen


def _accumulate_tool_call_deltas(
        store: dict[int, dict[str, Any]], deltas: list[dict[str, Any]]) -> None:
    """把 OpenAI 流式 tool_calls 分片按 index 合并成完整调用。"""
    for fallback_index, part in enumerate(deltas):
        if not isinstance(part, dict):
            continue
        try:
            index = int(part.get("index", fallback_index))
        except (TypeError, ValueError):
            index = fallback_index
        current = store.setdefault(index, {
            "id": "", "type": "function",
            "function": {"name": "", "arguments": ""},
        })
        if part.get("id"):
            current["id"] = str(part["id"])
        if part.get("type"):
            current["type"] = str(part["type"])
        fn = part.get("function") or {}
        if fn.get("name"):
            current["function"]["name"] += str(fn["name"])
        if fn.get("arguments"):
            current["function"]["arguments"] += str(fn["arguments"])


def _finalize_tool_calls(store: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for index in sorted(store):
        call = store[index]
        if not call.get("id"):
            call["id"] = f"call_{uuid.uuid4().hex[:24]}"
        calls.append(call)
    return calls


def _collect_chat(first: dict[str, Any], gen: Iterator[Any]) -> tuple[
        str, str, dict[str, Any] | None, str, list[dict[str, Any]]]:
    """在 worker 线程聚合同步上游流，避免阻塞 FastAPI 事件循环。"""
    content, reasoning = "", ""
    usage: dict[str, Any] | None = None
    finish = "stop"
    tool_call_store: dict[int, dict[str, Any]] = {}
    for chunk in _chain(first, gen):
        usage = chunk.get("usage") or usage
        for ch in chunk.get("choices") or []:
            d = ch.get("delta") or {}
            content += d.get("content") or ""
            reasoning += d.get("reasoning_content") or ""
            if d.get("tool_calls"):
                _accumulate_tool_call_deltas(tool_call_store, d["tool_calls"])
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    return content, reasoning, usage, finish, _finalize_tool_calls(tool_call_store)


def _norm_chunk(chunk: dict[str, Any], cid: str, created: int, model: str) -> dict[str, Any]:
    out = {
        "id": cid, "object": "chat.completion.chunk", "created": created,
        "model": chunk.get("model") or model,
        "choices": [],
    }
    for ch in chunk.get("choices") or []:
        d = ch.get("delta") or {}
        delta: dict[str, Any] = {}
        if d.get("role"):
            delta["role"] = d["role"]
        if d.get("content"):
            delta["content"] = d["content"]
        if d.get("reasoning_content"):
            delta["reasoning_content"] = d["reasoning_content"]
        if d.get("tool_calls"):
            delta["tool_calls"] = d["tool_calls"]
        out["choices"].append({"index": ch.get("index", 0), "delta": delta,
                               "finish_reason": ch.get("finish_reason") or None})
    if chunk.get("usage"):
        out["usage"] = chunk["usage"]
    return out


# --------------------------------------------------------------------------- #
# Anthropic 兼容
# --------------------------------------------------------------------------- #
def _anthropic_text(value: Any) -> str:
    """提取 Anthropic 文本块；tool_result 的正文也复用这条路径。"""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return "" if value is None else str(value)
    return "".join(
        str(block.get("text") or "")
        for block in value
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _anthropic_content_to_openai(value: Any) -> str | list[dict[str, Any]]:
    """把 Anthropic 的 text/image 内容块转换为 OpenAI content。"""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return "" if value is None else str(value)
    items: list[dict[str, Any]] = []
    for block in value:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            items.append({"type": "text", "text": str(block.get("text") or "")})
        elif btype == "image":
            source = block.get("source") or {}
            url = ""
            if source.get("type") == "base64" and source.get("data"):
                media_type = source.get("media_type") or "image/png"
                url = f"data:{media_type};base64,{source['data']}"
            elif source.get("type") == "url":
                url = str(source.get("url") or "")
            if url:
                items.append({"type": "image_url", "image_url": {"url": url}})
    if all(item.get("type") == "text" for item in items):
        return "".join(str(item.get("text") or "") for item in items)
    return items


def _anthropic_messages_to_openai(body: dict[str, Any]) -> list[dict[str, Any]]:
    """保真转换对话历史，尤其是 assistant.tool_use 与 user.tool_result。"""
    out: list[dict[str, Any]] = []
    system_text = _anthropic_text(body.get("system"))
    if system_text:
        out.append({"role": "system", "content": system_text})

    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        if not isinstance(content, list):
            out.append({"role": role, "content": _anthropic_content_to_openai(content)})
            continue

        if role == "assistant":
            text = _anthropic_text(content)
            calls: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                calls.append({
                    "id": str(block.get("id") or f"call_{uuid.uuid4().hex[:24]}"),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False,
                                                separators=(",", ":")),
                    },
                })
            item: dict[str, Any] = {"role": "assistant", "content": text}
            if calls:
                item["tool_calls"] = calls
            out.append(item)
            continue

        if role == "user":
            # OpenAI 把每个工具结果表示成独立的 role=tool 消息。
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                result = _anthropic_text(block.get("content"))
                if block.get("is_error"):
                    result = f"[tool_error] {result}"
                out.append({
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id") or ""),
                    "content": result,
                })
            normal_blocks = [block for block in content
                             if not (isinstance(block, dict)
                                     and block.get("type") == "tool_result")]
            normal_content = _anthropic_content_to_openai(normal_blocks)
            if normal_content not in ("", []):
                out.append({"role": "user", "content": normal_content})
            continue

        out.append({"role": role, "content": _anthropic_content_to_openai(content)})
    return out


def _anthropic_tools_to_openai(tools: Any) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        function: dict[str, Any] = {
            "name": str(tool["name"]),
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        }
        if tool.get("description") is not None:
            function["description"] = str(tool.get("description") or "")
        converted.append({"type": "function", "function": function})
    return converted


def _anthropic_tool_choice_to_openai(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return choice
    ctype = choice.get("type")
    if ctype == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": str(choice["name"])}}
    return {"auto": "auto", "any": "required", "none": "none"}.get(ctype, "auto")


def _anthropic_stop_reason(finish: str | None, has_tools: bool = False) -> str:
    if has_tools or finish in ("tool_calls", "function_call"):
        return "tool_use"
    return {
        "length": "max_tokens",
        "stop": "end_turn",
        "content_filter": "end_turn",
    }.get(finish or "stop", "end_turn")


def _anthropic_event(event: dict[str, Any]) -> str:
    return (f"event: {event['type']}\n"
            f"data: {json.dumps(event, ensure_ascii=False)}\n\n")


@app.post("/v1/messages", dependencies=[Depends(require_api)])
async def anthropic_messages(request: Request) -> Any:
    body = await request.json()
    model = body.get("model") or "default"
    want_stream = bool(body.get("stream"))

    payload: dict[str, Any] = {
        "model": model,
        "messages": _anthropic_messages_to_openai(body),
    }
    for key in ("temperature", "top_p", "max_tokens"):
        if key in body:
            payload[key] = body[key]
    if "stop_sequences" in body:
        payload["stop"] = body.get("stop_sequences") or []
    if "tools" in body:
        payload["tools"] = _anthropic_tools_to_openai(body.get("tools"))
    if "tool_choice" in body:
        payload["tool_choice"] = _anthropic_tool_choice_to_openai(body.get("tool_choice"))
    if "disable_parallel_tool_use" in body:
        payload["parallel_tool_calls"] = not bool(body.get("disable_parallel_tool_use"))

    acc = _pick_account(
        (request.headers.get("X-WB-Force-Account") or "").strip() or None
    )
    proxy = pm.pick()
    mid = f"msg_{uuid.uuid4().hex[:24]}"
    t0 = time.time()

    try:
        gen = upstream.stream_chat(acc.access_token, payload, proxy=proxy)
        # 和 OpenAI 路由一致：同步 httpx 首包不能阻塞 FastAPI 事件循环。
        first = await asyncio.to_thread(_next_upstream_chunk, gen)
        t_first = time.time()
    except upstream.UpstreamError as exc:
        pool.release(acc, error=f"{exc.code}: {exc.msg}")
        _log_call(request, model=model, ok=False, endpoint="messages", t0=t0,
                  account=acc.masked(), code=exc.code, error=exc.msg, stream=want_stream)
        raise HTTPException(exc.status if exc.status >= 400 else 502,
                            {"type": "error", "error": {"type": "api_error",
                                                          "message": exc.msg}})
    except _EmptyUpstreamStream:
        err = "上游返回空流"
        pool.release(acc, error=err)
        _log_call(request, model=model, ok=False, endpoint="messages", t0=t0,
                  account=acc.masked(), error=err, stream=want_stream)
        raise HTTPException(502, {"type": "error", "error": {
            "type": "api_error", "message": err,
        }})
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:200]
        if is_proxy_error(err) and proxy:
            pm.mark_bad(proxy)
            pool.release(acc)
        else:
            pool.release(acc, error=err)
        _log_call(request, model=model, ok=False, endpoint="messages", t0=t0,
                  account=acc.masked(), error=err, stream=want_stream)
        raise HTTPException(502, {"type": "error", "error": {
            "type": "api_error", "message": f"上游失败: {err}",
        }})

    if want_stream:
        def a_stream() -> Iterator[str]:
            usage: dict[str, Any] | None = None
            finish = "stop"
            text_index: int | None = None
            next_index = 0
            tool_call_store: dict[int, dict[str, Any]] = {}
            yield _anthropic_event({
                "type": "message_start",
                "message": {"id": mid, "type": "message", "role": "assistant",
                            "content": [], "model": model, "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0}},
            })
            try:
                for chunk in _chain(first, gen):
                    usage = chunk.get("usage") or usage
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = delta.get("content") or ""
                        if piece:
                            if text_index is None:
                                text_index = next_index
                                next_index += 1
                                yield _anthropic_event({
                                    "type": "content_block_start", "index": text_index,
                                    "content_block": {"type": "text", "text": ""},
                                })
                            yield _anthropic_event({
                                "type": "content_block_delta", "index": text_index,
                                "delta": {"type": "text_delta", "text": piece},
                            })
                        if delta.get("tool_calls"):
                            _accumulate_tool_call_deltas(tool_call_store,
                                                        delta["tool_calls"])
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]

                if text_index is not None:
                    yield _anthropic_event({
                        "type": "content_block_stop", "index": text_index,
                    })

                tool_calls = _finalize_tool_calls(tool_call_store)
                for call in tool_calls:
                    block_index = next_index
                    next_index += 1
                    function = call.get("function") or {}
                    arguments = str(function.get("arguments") or "{}")
                    yield _anthropic_event({
                        "type": "content_block_start", "index": block_index,
                        "content_block": {
                            "type": "tool_use", "id": call["id"],
                            "name": str(function.get("name") or ""), "input": {},
                        },
                    })
                    yield _anthropic_event({
                        "type": "content_block_delta", "index": block_index,
                        "delta": {"type": "input_json_delta", "partial_json": arguments},
                    })
                    yield _anthropic_event({
                        "type": "content_block_stop", "index": block_index,
                    })

                credit = ledger.record(model, usage)
                total_tokens = (usage or {}).get("total_tokens", 0)
                pool.release(acc, tokens=total_tokens, credits=credit)
                _log_call(request, model=model, ok=True, endpoint="messages", t0=t0,
                          tokens=total_tokens, credits=credit,
                          account=acc.masked(), stream=True, t_first=t_first,
                          out_tokens=(usage or {}).get("completion_tokens", 0))
                yield _anthropic_event({
                    "type": "message_delta",
                    "delta": {"stop_reason": _anthropic_stop_reason(
                        finish, bool(tool_calls)), "stop_sequence": None},
                    "usage": {"output_tokens": (usage or {}).get("completion_tokens", 0)},
                })
                yield _anthropic_event({"type": "message_stop"})
            except Exception as exc:  # noqa: BLE001
                err = str(exc)[:200]
                pool.release(acc, error=err)
                _log_call(request, model=model, ok=False, endpoint="messages", t0=t0,
                          account=acc.masked(), error=err, stream=True)
                yield _anthropic_event({
                    "type": "error",
                    "error": {"type": "api_error", "message": err},
                })

        return StreamingResponse(a_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no",
                                          "X-WB-Account": acc.masked()})

    try:
        text, _reasoning, usage, finish, tool_calls = await asyncio.to_thread(
            _collect_chat, first, gen)
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:200]
        pool.release(acc, error=err)
        _log_call(request, model=model, ok=False, endpoint="messages", t0=t0,
                  account=acc.masked(), error=err)
        raise HTTPException(502, {"type": "error", "error": {
            "type": "api_error", "message": f"上游流中断: {err}",
        }})

    content_blocks: list[dict[str, Any]] = []
    if text or not tool_calls:
        content_blocks.append({"type": "text", "text": text})
    for call in tool_calls:
        function = call.get("function") or {}
        raw_arguments = str(function.get("arguments") or "{}")
        try:
            tool_input = json.loads(raw_arguments)
        except json.JSONDecodeError:
            tool_input = {"_raw_arguments": raw_arguments}
        content_blocks.append({
            "type": "tool_use", "id": call["id"],
            "name": str(function.get("name") or ""), "input": tool_input,
        })

    credit = ledger.record(model, usage)
    total_tokens = (usage or {}).get("total_tokens", 0)
    pool.release(acc, tokens=total_tokens, credits=credit)
    _log_call(request, model=model, ok=True, endpoint="messages", t0=t0,
              tokens=total_tokens, credits=credit,
              account=acc.masked(), t_first=t_first,
              out_tokens=(usage or {}).get("completion_tokens", 0))
    return {
        "id": mid, "type": "message", "role": "assistant", "model": model,
        "content": content_blocks,
        "stop_reason": _anthropic_stop_reason(finish, bool(tool_calls)),
        "stop_sequence": None,
        "usage": {"input_tokens": (usage or {}).get("prompt_tokens", 0),
                  "output_tokens": (usage or {}).get("completion_tokens", 0)},
    }


# --------------------------------------------------------------------------- #
# 管理 API
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health(wb_session: str | None = Cookie(None, alias=COOKIE_NAME),
           authorization: str | None = Header(None),
           x_api_key: str | None = Header(None, alias="x-api-key")) -> dict[str, Any]:
    """存活探针公开；账号数/积分这些只在已登录时给，避免裸奔泄露池子规模。"""
    base = {"ok": True, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    authed = bool(webauth.session(wb_session or "")) or (
        bool(ADMIN_KEY) and _extract_key(authorization, x_api_key) == ADMIN_KEY)
    if not authed:
        return {**base, "authed": False}
    return {**base, "authed": True, "stats": pool.stats(), "proxy_mode": pm.mode,
            "api_key_required": keystore.has_any()}


# --------------------------------------------------------------------------- #
# 登录 / 会话
# --------------------------------------------------------------------------- #
def _cookie_secure(request: Request) -> bool:
    if COOKIE_SECURE == "on":
        return True
    if COOKIE_SECURE == "off":
        return False
    # auto：跟随当前请求的协议（经 1Panel/openresty 反代时看 X-Forwarded-Proto）
    xf = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return (xf or request.url.scheme) == "https"


@app.get("/api/auth/state")
def auth_state(request: Request,
               wb_session: str | None = Cookie(None, alias=COOKIE_NAME)) -> dict[str, Any]:
    """未登录也能调：前端据此决定显示登录页还是主界面。"""
    s = webauth.session(wb_session or "")
    return {
        "logged_in": bool(s),
        "user": (s or {}).get("user"),
        "default_password": webauth.uses_default_password(),
        "users": len(webauth.users()),
    }


@app.post("/api/auth/login")
async def auth_login(request: Request, response: Response) -> dict[str, Any]:
    body = await request.json()
    user = str(body.get("user") or "").strip()
    pwd = str(body.get("password") or "")
    tok = webauth.login(user, pwd, ua=request.headers.get("user-agent", ""))
    if not tok:
        raise HTTPException(401, "用户名或密码不对")
    response.set_cookie(COOKIE_NAME, tok, max_age=SESSION_TTL, httponly=True,
                        samesite="lax", secure=_cookie_secure(request), path="/")
    return {"ok": True, "user": user,
            "default_password": webauth.uses_default_password()}


@app.post("/api/auth/logout")
def auth_logout(response: Response,
                wb_session: str | None = Cookie(None, alias=COOKIE_NAME)) -> dict[str, Any]:
    webauth.logout(wb_session or "")
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.post("/api/auth/password")
async def auth_password(request: Request, response: Response,
                        user: str = Depends(require_admin)) -> dict[str, Any]:
    body = await request.json()
    target = user if user != "admin-key" else str(body.get("user") or "").strip()
    if not target:
        raise HTTPException(400, "缺少用户名")
    ok, msg = webauth.change_password(target, str(body.get("old") or ""),
                                      str(body.get("new") or ""))
    if not ok:
        raise HTTPException(400, msg)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True, "message": "密码已改，请重新登录"}


# --------------------------------------------------------------------------- #
# API Key 管理
# --------------------------------------------------------------------------- #
@app.get("/api/keys", dependencies=[Depends(require_admin)])
def api_keys_list() -> dict[str, Any]:
    return {"keys": keystore.public_list(), "prefix_note": "调用时作 Bearer token 或 x-api-key"}


@app.post("/api/keys", dependencies=[Depends(require_admin)])
async def api_keys_create(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    rec = keystore.create(name=str(body.get("name") or ""), note=str(body.get("note") or ""))
    # 明文只在这里返回一次，前端负责让用户复制
    return {"ok": True, "id": rec["id"], "name": rec["name"], "key": rec["key"]}


@app.post("/api/keys/{kid}/update", dependencies=[Depends(require_admin)])
async def api_keys_update(kid: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    if kid == "env":
        raise HTTPException(400, "环境变量里的 key 只能改 .env")
    ok = keystore.update(kid, name=body.get("name"), enabled=body.get("enabled"),
                         note=body.get("note"))
    if not ok:
        raise HTTPException(404, "key 不存在")
    return {"ok": True}


@app.post("/api/keys/{kid}/rotate", dependencies=[Depends(require_admin)])
def api_keys_rotate(kid: str) -> dict[str, Any]:
    if kid == "env":
        raise HTTPException(400, "环境变量里的 key 只能改 .env")
    nk = keystore.rotate(kid)
    if not nk:
        raise HTTPException(404, "key 不存在")
    return {"ok": True, "key": nk}


@app.delete("/api/keys/{kid}", dependencies=[Depends(require_admin)])
def api_keys_delete(kid: str) -> dict[str, Any]:
    if kid == "env":
        raise HTTPException(400, "环境变量里的 key 只能改 .env")
    if not keystore.delete(kid):
        raise HTTPException(404, "key 不存在")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 调用日志 / 模型可用性
# --------------------------------------------------------------------------- #
@app.get("/api/calls/health", dependencies=[Depends(require_admin)])
def api_calls_health(window_h: int = 24, buckets: int = 24,
                     include_known: bool = True) -> dict[str, Any]:
    known: list[str] = []
    if include_known:
        try:
            known = [m["id"] for m in (probe_models().get("models") or [])]
        except Exception:  # noqa: BLE001
            known = []
    return calllog.health(window_h=window_h, buckets=buckets, known_models=known)


@app.get("/api/calls/recent", dependencies=[Depends(require_admin)])
def api_calls_recent(limit: int = 80) -> dict[str, Any]:
    return {"calls": calllog.recent(limit=max(1, min(500, limit)))}


@app.post("/api/calls/reset", dependencies=[Depends(require_admin)])
def api_calls_reset() -> dict[str, Any]:
    calllog.reset()
    return {"ok": True}


@app.get("/api/pool", dependencies=[Depends(require_admin)])
def get_pool() -> dict[str, Any]:
    accs = []
    for a in pool.all():
        accs.append({
            "phone": a.phone, "masked": a.masked(), "uid": a.uid, "label": a.label,
            "status": a.status, "usable": a.usable(),
            "credits_total": a.credits_total, "credits_spent": a.credits_spent,
            "credits_checked_at": a.credits_checked_at,
            "request_count": a.request_count, "token_count": a.token_count,
            "last_used": a.last_used, "last_error": a.last_error,
            "last_checkin": a.last_checkin, "registered_at": a.registered_at,
            "expires_at": a.expires_at, "expires_in_h": round(a.expires_in() / 3600, 1),
            "cooldown_until": a.cooldown_until,
        })
    return {"stats": pool.stats(), "accounts": accs}



@app.get("/api/admin/accounts", dependencies=[Depends(require_admin)])
def get_accounts() -> dict[str, Any]:
    accs = []
    for a in pool.all():
        accs.append({
            "phone": a.phone,
            "masked": a.masked(),
            "credits_total": a.credits_total,
            "credits_spent": a.credits_spent,
            "registered_at": a.registered_at,
            "status": a.status,
        })
    return {"accounts": accs}

@app.post("/api/pool/refresh_balance", dependencies=[Depends(require_admin)])
def api_refresh_balance() -> dict[str, Any]:
    return {"ok": True, "results": pool.refresh_balances(proxy=pm.pick())}


@app.get("/api/pool/rotation", dependencies=[Depends(require_admin)])
def api_rotation_get() -> dict[str, Any]:
    return {"mode": pool.rotation_mode}


@app.post("/api/pool/rotation", dependencies=[Depends(require_admin)])
async def api_rotation_set(request: Request) -> dict[str, Any]:
    b = await request.json()
    ok, result = pool.set_rotation_mode(b.get("mode", ""))
    return {"ok": ok, "mode": result if ok else pool.rotation_mode, "error": "" if ok else result}


@app.post("/api/pool/checkin", dependencies=[Depends(require_admin)])
def api_checkin() -> dict[str, Any]:
    return {"ok": True, "results": pool.checkin_all(proxy=pm.pick())}


@app.post("/api/pool/checkin_one", dependencies=[Depends(require_admin)])
async def api_checkin_one(request: Request) -> dict[str, Any]:
    b = await request.json()
    return pool.checkin_one(b.get("phone", ""), proxy=pm.pick(),
                            force=bool(b.get("force")))


@app.post("/api/pool/status", dependencies=[Depends(require_admin)])
async def api_set_status(request: Request) -> dict[str, Any]:
    b = await request.json()
    ok = pool.set_status(b.get("phone", ""), b.get("status", "active"))
    return {"ok": ok}


@app.post("/api/pool/remove", dependencies=[Depends(require_admin)])
async def api_remove(request: Request) -> dict[str, Any]:
    b = await request.json()
    return {"ok": pool.remove(b.get("phone", ""))}


@app.post("/api/pool/refresh_token", dependencies=[Depends(require_admin)])
async def api_refresh_token(request: Request) -> dict[str, Any]:
    b = await request.json()
    acc = pool.find(b.get("phone", ""))
    if not acc:
        raise HTTPException(404, "账号不存在")
    if not acc.refresh_token:
        raise HTTPException(400, "该账号没有 refresh_token")
    ok = pool.try_refresh(acc, proxy=pm.pick())
    return {"ok": ok, "expires_in_h": round(acc.expires_in() / 3600, 1),
            "error": acc.last_error}


@app.post("/api/pool/import", dependencies=[Depends(require_admin)])
async def api_import(request: Request) -> dict[str, Any]:
    """手动导入已有 token（比如从桌面端 .info 文件里拿的）"""
    b = await request.json()
    at = (b.get("access_token") or "").strip()
    if not at:
        raise HTTPException(400, "access_token 必填")
    dec = upstream.decode_jwt(at)
    bal = upstream.get_balance(at, proxy=pm.pick(), retries=2)
    acc = Account(
        phone=b.get("phone", "") or f"import-{uuid.uuid4().hex[:6]}",
        uid=dec.get("sub", ""), access_token=at,
        refresh_token=(b.get("refresh_token") or "").strip(),
        expires_at=int(dec.get("exp", 0)) * 1000,
        credits_total=bal.get("total", -1.0),
        credits_checked_at=time.time() if bal.get("total", -1) >= 0 else 0.0,
        registered_at=bal.get("registered_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        label=b.get("label", "imported"), status="active")
    ok, how = pool.add(acc)
    return {"ok": ok, "action": how, "credits": bal.get("total"),
            "packages": bal.get("packages", []), "uid": acc.uid}


# ---- 注册（用户自己的手机号） ----
@app.post("/api/register/start", dependencies=[Depends(require_admin)])
async def api_reg_start(request: Request) -> dict[str, Any]:
    b = await request.json()
    res = registrar.start(b.get("phone", ""),
                          proxy_override=b.get("proxy") if "proxy" in b else None)
    if not res.get("ok"):
        return JSONResponse(res, status_code=400)
    return res


@app.post("/api/register/finish", dependencies=[Depends(require_admin)])
async def api_reg_finish(request: Request) -> dict[str, Any]:
    b = await request.json()
    res = registrar.finish(b.get("session_id", ""), b.get("code", ""),
                           b.get("label", ""), b.get("invite_code", ""))
    if not res.get("ok"):
        return JSONResponse(res, status_code=400)
    return res


@app.get("/api/register/sessions", dependencies=[Depends(require_admin)])
def api_reg_sessions() -> dict[str, Any]:
    return {"sessions": registrar.sessions()}


# ---- 自动注册（uoomsg） ----
@app.post("/api/auto_register/start", dependencies=[Depends(require_admin)])
async def api_auto_reg_start(request: Request) -> dict[str, Any]:
    if not UOOMSG_TOKEN:
        return JSONResponse({"ok": False, "error": "WB_UOOMSG_TOKEN 未配置"}, status_code=400)
    b = await request.json()
    return auto_registrar.start(
        invite_code=b.get("invite_code", ""),
        label=b.get("label", ""),
        count=b.get("count", 1),
    )


@app.post("/api/auto_register/stop/{task_id}", dependencies=[Depends(require_admin)])
def api_auto_reg_stop(task_id: str) -> dict[str, Any]:
    return auto_registrar.stop(task_id)


@app.get("/api/auto_register/status/{task_id}", dependencies=[Depends(require_admin)])
def api_auto_reg_status(task_id: str) -> dict[str, Any]:
    t = auto_registrar.get(task_id)
    if not t:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    return t


@app.get("/api/auto_register/tasks", dependencies=[Depends(require_admin)])
def api_auto_reg_tasks() -> dict[str, Any]:
    return {"tasks": auto_registrar.list_tasks()}


@app.post("/api/auto_register/clear", dependencies=[Depends(require_admin)])
def api_auto_reg_clear() -> dict[str, Any]:
    return auto_registrar.clear_finished()


@app.get("/api/uoomsg/balance", dependencies=[Depends(require_admin)])
def api_uoomsg_balance() -> dict[str, Any]:
    if not UOOMSG_TOKEN:
        return {"ok": False, "error": "WB_UOOMSG_TOKEN 未配置"}
    bal = uum.balance(UOOMSG_TOKEN)
    return {"ok": True, "balance": bal}


# ---- 模型 / 倍率 ----
@app.get("/api/models", dependencies=[Depends(require_admin)])
def api_models(force: bool = False) -> dict[str, Any]:
    c = probe_models(force=force)
    models = c.get("models") or []
    return {"models": [m["id"] for m in models], "details": models,
            "meta": {m["id"]: m for m in models},
            "source": c.get("source"), "probed_at": c.get("probed_at"),
            "error": c.get("error")}


@app.get("/api/rates", dependencies=[Depends(require_admin)])
def api_rates() -> dict[str, Any]:
    """
    倍率表。上游没有倍率/价格接口（所有 rate|price|model-* 路径实测 404），
    但 chat 响应的 usage 里带 **credit** 字段 —— 那是本次请求真实扣的积分。
    本表按 model 累计 credit 与 tokens 算出，样本越多越准。
    """
    t = ledger.table()
    models, _ = resolve_models(MODELS_CACHE)
    t["meta"] = {m["id"]: m for m in models}
    return t


@app.post("/api/rates/measure", dependencies=[Depends(require_admin)])
async def api_measure_rates(request: Request) -> dict[str, Any]:
    """
    主动跑一轮实测：对每个可用模型发一次固定 prompt，直接读 usage.credit 记账。
    不需要等余额异步结算，也不需要余额差归因。
    """
    try:
        b = await request.json()
    except Exception:  # noqa: BLE001
        b = {}
    models = b.get("models") or [m["id"] for m in resolve_models(MODELS_CACHE)[0]]
    prompt = b.get("prompt") or "请用中文写一段约120字的短文，介绍春天。"

    acc = _pick_account()
    proxy = pm.pick()
    results = []
    for m in models:
        if m in ("default", "auto"):
            continue
        text, usage = "", None
        try:
            for chunk in upstream.stream_chat(acc.access_token,
                    {"model": m, "messages": [{"role": "user", "content": prompt}]},
                    proxy=proxy):
                usage = chunk.get("usage") or usage
                for ch in chunk.get("choices") or []:
                    text += (ch.get("delta") or {}).get("content") or ""
        except Exception as exc:  # noqa: BLE001
            results.append({"model": m, "error": str(exc)[:150]})
            continue
        credit = ledger.record(m, usage)
        tt = (usage or {}).get("total_tokens")
        results.append({
            "model": m, "credit": credit, "total_tokens": tt,
            "prompt_tokens": (usage or {}).get("prompt_tokens"),
            "completion_tokens": (usage or {}).get("completion_tokens"),
            "credits_per_1k": round(credit / (tt / 1000), 5) if (credit and tt) else None,
            "sample": text[:60],
        })
    pool.release(acc)
    return {"ok": True, "results": results, "table": ledger.table()}


@app.post("/api/rates/reset", dependencies=[Depends(require_admin)])
def api_rates_reset() -> dict[str, Any]:
    ledger.reset()
    return {"ok": True}


# ---- 代理 ----
@app.get("/api/proxy", dependencies=[Depends(require_admin)])
def api_proxy() -> dict[str, Any]:
    return pm.status()


@app.post("/api/proxy/probe", dependencies=[Depends(require_admin)])
def api_proxy_probe() -> dict[str, Any]:
    return {"ok": True, "results": pm.probe_all(force=True), "usable": pm.usable_ports()}


@app.post("/api/proxy/mode", dependencies=[Depends(require_admin)])
async def api_proxy_mode(request: Request) -> dict[str, Any]:
    b = await request.json()
    mode = b.get("mode", "off")
    if mode not in ("off", "fixed", "rotate"):
        raise HTTPException(400, "mode 必须是 off|fixed|rotate")
    pm.mode = mode
    if "url" in b:
        pm.fixed_url = b["url"] or ""
    return {"ok": True, "mode": pm.mode, "fixed_url": pm.fixed_url}


# ---- 邀请 ----
@app.get("/api/invite", dependencies=[Depends(require_admin)])
def api_invite(phone: str = "") -> dict[str, Any]:
    """某个账号的邀请总览：邀请码、邀请链接、拉新进度、已得奖励、好友记录。"""
    acc = pool.find(phone) if phone else (pool.all()[0] if pool.all() else None)
    if not acc:
        raise HTTPException(404, "账号不存在")
    d = invite_mod.overview(acc.access_token, proxy=pm.pick())
    d.update({"phone": acc.phone, "masked": acc.masked()})
    return d


@app.get("/api/invite/codes", dependencies=[Depends(require_admin)])
def api_invite_codes() -> dict[str, Any]:
    """
    池里所有账号的邀请码 —— 注册新号时从这里挑一个填进注册表单。
    自己的码不能绑自己（服务端 12313），所以每个条目带上 phone 供前端排除。
    """
    proxy = pm.pick()
    out = []
    for a in pool.all():
        if not a.usable():
            out.append({"phone": a.phone, "masked": a.masked(),
                        "code": "", "error": f"账号状态 {a.status}"})
            continue
        try:
            d = invite_mod.overview(a.access_token, proxy=proxy)
            out.append({
                "phone": a.phone, "masked": a.masked(), "label": a.label,
                "code": d.get("invite_code", ""),
                "link": d.get("invite_link", ""),
                "invited": d.get("invite_count", 0),
                "valid_invited": d.get("valid_invite_count", 0),
                "earned": d.get("total_credits", 0),
                "cap": d.get("cap_value", 30000),
                "cap_reached": d.get("cap_reached", False),
            })
        except Exception as exc:  # noqa: BLE001
            out.append({"phone": a.phone, "masked": a.masked(),
                        "code": "", "error": str(exc)[:120]})
    return {"codes": out,
            "note": "注册新账号时填其中一个码，注册成功后邀请人得奖励；不能填被注册号自己的码"}


@app.post("/api/invite/bind", dependencies=[Depends(require_admin)])
async def api_invite_bind(request: Request) -> dict[str, Any]:
    """
    给池里某个已有账号补绑邀请码（当时注册忘填的场景）。
    body: {"phone": "...", "invite_code": "..."}
    """
    b = await request.json()
    acc = pool.find(b.get("phone", ""))
    if not acc:
        raise HTTPException(404, "账号不存在")
    r = invite_mod.bind(acc.access_token, b.get("invite_code", ""), proxy=pm.pick())
    if r.get("ok"):
        pool.refresh_balance(acc, proxy=pm.pick())
    return r


# --------------------------------------------------------------------------- #
# 定时任务
# --------------------------------------------------------------------------- #
def _job_checkin() -> None:
    try:
        pool.checkin_all(proxy=pm.pick())
    except Exception:  # noqa: BLE001
        pass


def _job_balance() -> None:
    try:
        pool.refresh_balances(proxy=pm.pick())
    except Exception:  # noqa: BLE001
        pass


def _job_sync_models() -> None:
    try:
        r = probe_models(force=True)
        print(f"[cron] 模型清单刷新 source={r.get('source')} "
              f"count={len(r.get('models') or [])} error={r.get('error')}")
    except Exception as exc:  # noqa: BLE001
        print(f"[cron] 模型清单刷新失败: {exc}")


@app.on_event("startup")
def _startup() -> None:
    # 模型清单：缓存过期则拉一次官方 console 接口（失败自动落静态兜底）
    if is_cache_expired(MODELS_CACHE):
        r = probe_models(force=True)
        print(f"[startup] 模型清单 source={r.get('source')} "
              f"count={len(r.get('models') or [])} error={r.get('error')}")
    else:
        c = load_sync_cache(MODELS_CACHE)
        print(f"[startup] 模型缓存有效，{c.get('available_count')} 个模型，"
              f"synced_at={c.get('synced_at')}")

    # 每 6h 自动刷新一次模型清单
    scheduler.add_job(_job_sync_models, "interval", hours=6,
                      id="sync_models", name="模型清单刷新", replace_existing=True)
    
    # 添加定时任务
    try:
        mi, h, d, mo, dow = CHECKIN_CRON.split()
        scheduler.add_job(_job_checkin, CronTrigger(minute=mi, hour=h, day=d, month=mo,
                                                    day_of_week=dow, timezone="Asia/Shanghai"),
                          id="checkin", name="每日签到", replace_existing=True)
    except Exception:  # noqa: BLE001
        pass
    scheduler.add_job(_job_balance, "interval", minutes=BALANCE_INTERVAL,
                      id="balance", name="余额刷新", replace_existing=True)
    scheduler.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    try:
        scheduler.shutdown(wait=False)
    except Exception:  # noqa: BLE001
        pass


@app.get("/api/scheduler", dependencies=[Depends(require_admin)])
def api_scheduler() -> dict[str, Any]:
    return {"jobs": [{"id": j.id, "name": j.name,
                      "next_run": str(getattr(j, "next_run_time", ""))}
                     for j in scheduler.get_jobs()]}


# ---- 模型同步 ----
@app.post("/api/admin/sync-models", dependencies=[Depends(require_admin)])
async def api_sync_models(request: Request) -> dict[str, Any]:
    """从上游同步模型列表，更新 models_cache.json"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    
    r = probe_models(force=True)
    models = r.get("models") or []
    return {"ok": r.get("error") is None, "source": r.get("source"),
            "available_count": len(models),
            "models": [m["id"] for m in models],
            "details": models, "error": r.get("error")}


@app.get("/api/admin/models-cache-status", dependencies=[Depends(require_admin)])
def api_models_cache_status() -> dict[str, Any]:
    """检查模型缓存状态"""
    if not MODELS_CACHE.exists():
        return {
            "exists": False,
            "expired": True,
            "source": "static",
            "available_count": len(static_models()),
            "static_fallback_count": len(static_models()),
            "message": "缓存不存在，/v1/models 走静态兜底表，可点同步",
        }
    
    try:
        data = load_sync_cache(MODELS_CACHE)
        return {
            "exists": True,
            "expired": is_cache_expired(MODELS_CACHE),
            "synced_at": data.get("synced_at"),
            "source": data.get("source"),
            "available_count": data.get("available_count", 0),
            "age_hours": round((time.time() - data.get("timestamp", 0)) / 3600, 1),
            "last_error": data.get("last_error"),
            "in_fail_cooldown": in_fail_cooldown(data),
            "static_fallback_count": len(static_models()),
        }
    except Exception as e:
        return {
            "exists": True,
            "expired": True,
            "error": str(e),
            "message": "缓存损坏，需要重新同步",
        }


# --------------------------------------------------------------------------- #
# WebUI
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Admin API - 账号管理
# --------------------------------------------------------------------------- #
@app.get("/api/admin/accounts", dependencies=[Depends(require_admin)])
def api_get_accounts() -> dict[str, Any]:
    """获取所有账号信息（含积分、注册时间）"""
    accounts_data = []
    for acc in pool._accounts:
        accounts_data.append({
            "phone": acc.phone,
            "points": acc.credits_total,
            "status": acc.status,
            "registered_at": acc.registered_at.isoformat() if acc.registered_at else None,
            "expires_at": acc.expires_at.isoformat() if acc.expires_at else None,
            "last_checkin": acc.last_checkin.isoformat() if acc.last_checkin else None,
        })
    
    return {
        "total": len(accounts_data),
        "accounts": accounts_data,
    }

STATIC_DIR = BASE_DIR / "web"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _asset_ver() -> str:
    """按 app.css / app.js 的 mtime 生成版本号。

    StaticFiles 只发 ETag/Last-Modified，浏览器（尤其带 SW 或强缓存的）改完前端
    还会拿旧文件，看着像"改了没生效"。给 URL 带上随文件变化的 v= 参数，
    发版后第一次请求必然 miss，不用叫用户清缓存。
    """
    ts = 0.0
    for name in ("app.css", "app.js", "chat-utils.js", "favicon.png"):
        f = STATIC_DIR / name
        if f.exists():
            ts = max(ts, f.stat().st_mtime)
    return hex(int(ts))[2:] or "0"


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    f = STATIC_DIR / "index.html"
    if f.exists():
        html = f.read_text(encoding="utf-8")
        v = _asset_ver()
        html = (html.replace("/static/app.css", f"/static/app.css?v={v}")
                    .replace("/static/chat-utils.js", f"/static/chat-utils.js?v={v}")
                    .replace("/static/app.js", f"/static/app.js?v={v}")
                    .replace("/static/favicon.png", f"/static/favicon.png?v={v}"))
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})
    return HTMLResponse("<h1>wb-pool</h1><p>WebUI 未安装</p>")
