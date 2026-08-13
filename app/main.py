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

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import upstream
from . import invite as invite_mod
from .accounting import Ledger
from .pool import Account, AccountPool
from .proxies import DEFAULT_EXITS, ProxyManager
from .register import Registrar

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("WB_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

ACCOUNTS_FILE = Path(os.environ.get("WB_ACCOUNTS_FILE", DATA_DIR / "accounts.jsonl"))
MODELS_CACHE = DATA_DIR / "models_cache.json"
PROXY_STATE = DATA_DIR / "proxy_state.json"

API_KEY = os.environ.get("WB_API_KEY", "")            # 反代访问密钥（空 = 不校验）
ADMIN_KEY = os.environ.get("WB_ADMIN_KEY", API_KEY)   # 管理接口密钥
PROXY_MODE = os.environ.get("WB_PROXY_MODE", "off")   # off | fixed | rotate
PROXY_HOST = os.environ.get("WB_PROXY_HOST", "127.0.0.1")
PROXY_FIXED = os.environ.get("WB_PROXY_URL", "")
CHECKIN_CRON = os.environ.get("WB_CHECKIN_CRON", "5 1 * * *")
BALANCE_INTERVAL = int(os.environ.get("WB_BALANCE_INTERVAL_MIN", "30"))

pool = AccountPool(ACCOUNTS_FILE)
ledger = Ledger(DATA_DIR / "ledger.json")
pm = ProxyManager(mode=PROXY_MODE, host=PROXY_HOST, fixed_url=PROXY_FIXED,
                  exits=DEFAULT_EXITS, state_file=PROXY_STATE)
registrar = Registrar(pool, pm)
scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

app = FastAPI(title="wb-pool", version="1.0.0", docs_url="/api/docs", redoc_url=None)


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #
def _extract_key(authorization: str | None, x_api_key: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return (x_api_key or "").strip()


def require_api(authorization: str | None = Header(None),
                x_api_key: str | None = Header(None, alias="x-api-key")) -> None:
    if not API_KEY:
        return
    if _extract_key(authorization, x_api_key) != API_KEY:
        raise HTTPException(401, "invalid api key")


def require_admin(authorization: str | None = Header(None),
                  x_api_key: str | None = Header(None, alias="x-api-key")) -> None:
    if not ADMIN_KEY:
        return
    if _extract_key(authorization, x_api_key) != ADMIN_KEY:
        raise HTTPException(401, "invalid admin key")


# --------------------------------------------------------------------------- #
# 模型缓存
# --------------------------------------------------------------------------- #
def load_models_cache() -> dict[str, Any]:
    if MODELS_CACHE.exists():
        try:
            return json.loads(MODELS_CACHE.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {"models": [], "probed_at": 0, "details": []}


def save_models_cache(d: dict[str, Any]) -> None:
    tmp = MODELS_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2))
    tmp.replace(MODELS_CACHE)


def probe_models(force: bool = False) -> dict[str, Any]:
    cache = load_models_cache()
    if cache["models"] and not force and (time.time() - cache.get("probed_at", 0)) < 3600:
        return cache
    acc = pool.acquire(proxy=pm.pick())
    if not acc:
        return cache | {"error": "池中无可用账号"}
    import concurrent.futures as cf
    proxy = pm.pick()
    details: list[dict[str, Any]] = []
    with cf.ThreadPoolExecutor(4) as ex:
        futs = [ex.submit(upstream.probe_model, acc.access_token, m, proxy)
                for m in upstream.MODEL_CANDIDATES]
        for f in futs:
            details.append(f.result())
    ok = [d["model"] for d in details if d.get("available")]
    out = {"models": ok, "details": details, "probed_at": time.time()}
    save_models_cache(out)
    return out


# --------------------------------------------------------------------------- #
# OpenAI 兼容
# --------------------------------------------------------------------------- #
@app.get("/v1/models", dependencies=[Depends(require_api)])
def list_models() -> dict[str, Any]:
    cache = load_models_cache()
    models = cache.get("models") or [m for m in upstream.MODEL_META]
    now = int(time.time())
    return {
        "object": "list",
        "data": [{
            "id": m, "object": "model", "created": now, "owned_by":
                upstream.MODEL_META.get(m, {}).get("vendor", "codebuddy"),
            "meta": upstream.MODEL_META.get(m, {}),
        } for m in models],
    }


def _sse(obj: Any) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _pick_account() -> Account:
    acc = pool.acquire(proxy=pm.pick())
    if not acc:
        raise HTTPException(503, "账号池中没有可用账号，请先在 WebUI 添加账号")
    return acc


@app.post("/v1/chat/completions", dependencies=[Depends(require_api)])
async def chat_completions(request: Request) -> Any:
    body = await request.json()
    want_stream = bool(body.get("stream"))
    model = body.get("model") or "default"
    payload = {k: v for k, v in body.items() if k not in ("stream", "stream_options")}
    payload["model"] = model

    last_err: str | None = None
    tried: list[str] = []
    for _ in range(min(3, max(1, len(pool.all())))):
        acc = _pick_account()
        if acc.masked() in tried:
            continue
        tried.append(acc.masked())
        proxy = pm.pick()
        try:
            gen = upstream.stream_chat(acc.access_token, payload, proxy=proxy)
            first = next(gen)          # 触发首包，才能捕获上游错误
        except upstream.UpstreamError as exc:
            last_err = f"{exc.code}: {exc.msg}"
            pool.release(acc, error=last_err)
            if exc.code == 11102:      # 模型不存在，换号也没用
                raise HTTPException(400, {"error": {"message": exc.msg, "code": exc.code,
                                                    "type": "invalid_request_error"}})
            continue
        except StopIteration:
            last_err = "上游返回空流"
            pool.release(acc, error=last_err)
            continue
        except Exception as exc:       # noqa: BLE001
            last_err = str(exc)[:200]
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
                    pool.release(acc, tokens=(usage or {}).get("total_tokens", 0),
                                 credits=credit)
                except Exception as exc:  # noqa: BLE001
                    pool.release(acc, error=str(exc)[:200])
                    yield _sse({"error": {"message": str(exc)[:200], "type": "upstream_error"}})
                    yield "data: [DONE]\n\n"

            return StreamingResponse(event_stream(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no",
                                              "X-WB-Account": acc.masked()})

        # 非流式：聚合上游流
        content, reasoning, usage, finish, tool_calls = "", "", None, "stop", []
        try:
            for chunk in _chain(first, gen):
                usage = chunk.get("usage") or usage
                for ch in chunk.get("choices") or []:
                    d = ch.get("delta") or {}
                    content += d.get("content") or ""
                    reasoning += d.get("reasoning_content") or ""
                    if d.get("tool_calls"):
                        tool_calls.extend(d["tool_calls"])
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
        except Exception as exc:  # noqa: BLE001
            pool.release(acc, error=str(exc)[:200])
            raise HTTPException(502, f"上游流中断: {exc}"[:200])

        credit = ledger.record(model, usage)
        pool.release(acc, tokens=(usage or {}).get("total_tokens", 0), credits=credit)
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
@app.post("/v1/messages", dependencies=[Depends(require_api)])
async def anthropic_messages(request: Request) -> Any:
    body = await request.json()
    model = body.get("model") or "default"
    want_stream = bool(body.get("stream"))

    msgs: list[dict[str, Any]] = []
    sysp = body.get("system")
    if isinstance(sysp, str) and sysp:
        msgs.append({"role": "system", "content": sysp})
    elif isinstance(sysp, list):
        txt = "".join(b.get("text", "") for b in sysp if isinstance(b, dict))
        if txt:
            msgs.append({"role": "system", "content": txt})
    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, list):
            c = "".join(b.get("text", "") for b in c
                        if isinstance(b, dict) and b.get("type") == "text")
        msgs.append({"role": m.get("role", "user"), "content": c or ""})

    payload = {"model": model, "messages": msgs}
    for k in ("temperature", "top_p", "max_tokens"):
        if k in body:
            payload[k] = body[k]

    acc = _pick_account()
    proxy = pm.pick()
    mid = f"msg_{uuid.uuid4().hex[:24]}"

    try:
        gen = upstream.stream_chat(acc.access_token, payload, proxy=proxy)
        first = next(gen)
    except upstream.UpstreamError as exc:
        pool.release(acc, error=f"{exc.code}: {exc.msg}")
        raise HTTPException(exc.status if exc.status >= 400 else 502,
                            {"type": "error", "error": {"type": "api_error", "message": exc.msg}})
    except Exception as exc:  # noqa: BLE001
        pool.release(acc, error=str(exc)[:200])
        raise HTTPException(502, f"上游失败: {exc}"[:200])

    if want_stream:
        def a_stream() -> Iterator[str]:
            usage = None
            yield ("event: message_start\ndata: " + json.dumps({
                "type": "message_start",
                "message": {"id": mid, "type": "message", "role": "assistant",
                            "content": [], "model": model, "stop_reason": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0}}}) + "\n\n")
            yield ("event: content_block_start\ndata: " + json.dumps({
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""}}) + "\n\n")
            try:
                for chunk in _chain(first, gen):
                    usage = chunk.get("usage") or usage
                    for ch in chunk.get("choices") or []:
                        piece = (ch.get("delta") or {}).get("content") or ""
                        if piece:
                            yield ("event: content_block_delta\ndata: " + json.dumps({
                                "type": "content_block_delta", "index": 0,
                                "delta": {"type": "text_delta", "text": piece}},
                                ensure_ascii=False) + "\n\n")
                pool.release(acc, tokens=(usage or {}).get("total_tokens", 0),
                             credits=ledger.record(model, usage))
            except Exception as exc:  # noqa: BLE001
                pool.release(acc, error=str(exc)[:200])
            yield "event: content_block_stop\ndata: " + json.dumps(
                {"type": "content_block_stop", "index": 0}) + "\n\n"
            yield ("event: message_delta\ndata: " + json.dumps({
                "type": "message_delta", "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": (usage or {}).get("completion_tokens", 0)}}) + "\n\n")
            yield "event: message_stop\ndata: " + json.dumps({"type": "message_stop"}) + "\n\n"

        return StreamingResponse(a_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    text, usage = "", None
    try:
        for chunk in _chain(first, gen):
            usage = chunk.get("usage") or usage
            for ch in chunk.get("choices") or []:
                text += (ch.get("delta") or {}).get("content") or ""
    except Exception as exc:  # noqa: BLE001
        pool.release(acc, error=str(exc)[:200])
        raise HTTPException(502, f"上游流中断: {exc}"[:200])
    pool.release(acc, tokens=(usage or {}).get("total_tokens", 0),
                 credits=ledger.record(model, usage))
    return {
        "id": mid, "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": (usage or {}).get("prompt_tokens", 0),
                  "output_tokens": (usage or {}).get("completion_tokens", 0)},
    }


# --------------------------------------------------------------------------- #
# 管理 API
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "stats": pool.stats(), "proxy_mode": pm.mode,
            "api_key_required": bool(API_KEY), "time": time.strftime("%Y-%m-%d %H:%M:%S")}


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


@app.post("/api/pool/refresh_balance", dependencies=[Depends(require_admin)])
def api_refresh_balance() -> dict[str, Any]:
    return {"ok": True, "results": pool.refresh_balances(proxy=pm.pick())}


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
        registered_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
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


# ---- 模型 / 倍率 ----
@app.get("/api/models", dependencies=[Depends(require_admin)])
def api_models(force: bool = False) -> dict[str, Any]:
    c = probe_models(force=force)
    return {"models": c.get("models", []), "details": c.get("details", []),
            "probed_at": c.get("probed_at"), "meta": upstream.MODEL_META,
            "error": c.get("error")}


@app.get("/api/rates", dependencies=[Depends(require_admin)])
def api_rates() -> dict[str, Any]:
    """
    倍率表。上游没有倍率/价格接口（所有 rate|price|model-* 路径实测 404），
    但 chat 响应的 usage 里带 **credit** 字段 —— 那是本次请求真实扣的积分。
    本表按 model 累计 credit 与 tokens 算出，样本越多越准。
    """
    t = ledger.table()
    t["meta"] = upstream.MODEL_META
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
    models = b.get("models") or load_models_cache().get("models") or []
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


@app.on_event("startup")
def _startup() -> None:
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


# --------------------------------------------------------------------------- #
# WebUI
# --------------------------------------------------------------------------- #
STATIC_DIR = BASE_DIR / "web"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    f = STATIC_DIR / "index.html"
    if f.exists():
        return HTMLResponse(f.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>wb-pool</h1><p>WebUI 未安装</p>")
