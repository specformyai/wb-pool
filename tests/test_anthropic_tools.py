#!/usr/bin/env python3
"""Anthropic /v1/messages 的工具调用协议回归测试（假上游，不消耗真实积分）。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from starlette.requests import Request

TD = tempfile.mkdtemp(prefix="wb-anthropic-tools-")
os.environ.update({
    "WB_DATA_DIR": TD,
    "WB_ACCOUNTS_FILE": str(Path(TD) / "accounts.jsonl"),
    "WB_API_KEY": "",
    "WB_ADMIN_KEY": "",
    "WB_PROXY_MODE": "off",
    "WB_CHECKIN_CRON": "",
    "WB_BALANCE_INTERVAL_MIN": "9999",
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app import main as M
from app.pool import Account


def sse_events(response: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


def sse_event_names(response: Any) -> list[str]:
    return [line[7:] for line in response.text.splitlines()
            if line.startswith("event: ")]



def tool_use_from(events: list[dict[str, Any]]) -> dict[str, Any]:
    starts = [e["content_block"] for e in events
              if e.get("type") == "content_block_start"
              and (e.get("content_block") or {}).get("type") == "tool_use"]
    assert len(starts) == 1, starts
    block = starts[0]
    partial = "".join(
        (e.get("delta") or {}).get("partial_json", "")
        for e in events
        if e.get("type") == "content_block_delta"
        and (e.get("delta") or {}).get("type") == "input_json_delta"
    )
    return {**block, "input": json.loads(partial)}


captured: list[dict[str, Any]] = []


def fake_stream(token: str, payload: dict[str, Any], proxy: str | None = None,
                timeout: float = 300.0):
    captured.append(payload)
    if any(m.get("role") == "tool" for m in payload.get("messages") or []):
        yield {"choices": [{"index": 0, "delta": {"role": "assistant"},
                            "finish_reason": None}]}
        yield {"choices": [{"index": 0, "delta": {"content": "杭州当前 25°C，晴。"},
                            "finish_reason": None}]}
        yield {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38}}
        return

    yield {"choices": [{"index": 0, "delta": {"role": "assistant"},
                        "finish_reason": None}]}
    yield {"choices": [{"index": 0, "delta": {"tool_calls": [{
        "index": 0,
        "id": "call_weather_1",
        "type": "function",
        "function": {"name": "lookup_weather", "arguments": ""},
    }]}, "finish_reason": None}]}
    yield {"choices": [{"index": 0, "delta": {"tool_calls": [{
        "index": 0, "function": {"name": "", "arguments": "{\"city\":\"杭"},
    }]}, "finish_reason": None}]}
    yield {"choices": [{"index": 0, "delta": {"tool_calls": [{
        "index": 0, "function": {"name": "", "arguments": "州\"}"},
    }]}, "finish_reason": None}]}
    yield {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
           "usage": {"prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32}}


seed = Account(phone="+8613800000001", status="active", credits_total=100.0,
               access_token="test-token", expires_at=int((time.time() + 86400) * 1000))
ORIGINAL_STREAM_CHAT = M.upstream.stream_chat
M.upstream.stream_chat = fake_stream

tool = {
    "name": "lookup_weather",
    "description": "查询指定城市天气",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    },
}

# 多模态工具结果不能被静默丢成空字符串。
converted_image_result = M._anthropic_messages_to_openai({
    "messages": [{"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": "call_image_1",
        "content": [
            {"type": "text", "text": "截图如下"},
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": "AA==",
            }},
        ],
    }]}],
})
assert converted_image_result == [{
    "role": "tool",
    "tool_call_id": "call_image_1",
    "content": [
        {"type": "text", "text": "截图如下"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ],
}], converted_image_result

with TestClient(M.app) as client:
    # 启动阶段保持空池，避免测试访问真实模型接口；进入请求阶段才注入假账号。
    M.pool._accounts = [seed]
    # 第一轮：Anthropic 工具定义必须传到 OpenAI 上游，并返回结构化 tool_use。
    first = client.post("/v1/messages", json={
        "model": "hy3",
        "stream": True,
        "max_tokens": 1024,
        "system": [{"type": "text", "text": "你是天气助手。"}],
        "messages": [{"role": "user", "content": "查询杭州天气"}],
        "tools": [
            tool,
            {
                "name": "lookup_time",
                "description": "查询时间",
                "input_schema": {"type": "object", "properties": {}},
            },
        ],
        "tool_choice": {
            "type": "tool", "name": "lookup_weather",
            "disable_parallel_tool_use": True,
        },
    })
    assert first.status_code == 200, first.text
    sent = captured[0]
    assert sent["tools"] == [{
        "type": "function",
        "function": {
            "name": "lookup_weather",
            "description": "查询指定城市天气",
            "parameters": tool["input_schema"],
        },
    }], sent
    assert sent["tool_choice"] == "required", sent
    assert sent["parallel_tool_calls"] is False, sent
    assert sent["messages"] == [
        {"role": "system", "content": "你是天气助手。"},
        {"role": "user", "content": "查询杭州天气"},
    ], sent["messages"]

    first_events = sse_events(first)
    first_types = [event.get("type") for event in first_events]
    assert sse_event_names(first) == first_types, first.text
    assert first_types == [
        "message_start", "content_block_start", "content_block_delta",
        "content_block_stop", "message_delta", "message_stop",
    ], first.text
    tool_use = tool_use_from(first_events)
    assert tool_use == {
        "type": "tool_use", "id": "call_weather_1",
        "name": "lookup_weather", "input": {"city": "杭州"},
    }, tool_use
    assert any(e.get("type") == "message_delta"
               and (e.get("delta") or {}).get("stop_reason") == "tool_use"
               for e in first_events), first.text
    assert "<tool_use>" not in first.text

    # 第二轮：assistant.tool_use + user.tool_result 必须转回 OpenAI tool_calls/tool message，
    # 模型收到工具结果后继续给最终文本，而不是中断或把结果泄漏成对话正文。
    second = client.post("/v1/messages", json={
        "model": "hy3",
        "stream": True,
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "查询杭州天气"},
            {"role": "assistant", "content": [tool_use]},
            {"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use["id"],
                "content": [{"type": "text", "text": "25°C，晴"}],
            }]},
        ],
        "tools": [tool],
        "tool_choice": {"type": "auto"},
    })
    assert second.status_code == 200, second.text
    sent2 = captured[1]
    assert sent2["messages"] == [
        {"role": "user", "content": "查询杭州天气"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_weather_1", "type": "function",
            "function": {"name": "lookup_weather", "arguments": "{\"city\":\"杭州\"}"},
        }]},
        {"role": "tool", "tool_call_id": "call_weather_1", "content": "25°C，晴"},
    ], sent2["messages"]
    second_events = sse_events(second)
    text = "".join(
        (e.get("delta") or {}).get("text", "")
        for e in second_events
        if (e.get("delta") or {}).get("type") == "text_delta"
    )
    assert text == "杭州当前 25°C，晴。", text
    assert any(e.get("type") == "message_delta"
               and (e.get("delta") or {}).get("stop_reason") == "end_turn"
               for e in second_events), second.text

    # 非流式也必须把分片 tool_call 合并成一个合法 Anthropic tool_use。
    nonstream = client.post("/v1/messages", json={
        "model": "hy3", "stream": False, "max_tokens": 1024,
        "messages": [{"role": "user", "content": "查询杭州天气"}],
        "tools": [tool],
    })
    assert nonstream.status_code == 200, nonstream.text
    body = nonstream.json()
    assert body["stop_reason"] == "tool_use", body
    assert body["content"] == [{
        "type": "tool_use", "id": "call_weather_1",
        "name": "lookup_weather", "input": {"city": "杭州"},
    }], body

    # 首包前的上游错误必须直接使用 Anthropic 错误对象，不能被 FastAPI 包进 detail。
    def fake_error(token: str, payload: dict[str, Any], proxy: str | None = None,
                   timeout: float = 300.0):
        raise M.upstream.UpstreamError(400, "bad_tool_choice", "bad tool choice")

    M.upstream.stream_chat = fake_error
    error_response = client.post("/v1/messages", json={
        "model": "hy3", "max_tokens": 32,
        "messages": [{"role": "user", "content": "test"}],
    })
    assert error_response.status_code == 400, error_response.text
    assert error_response.json() == {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "bad tool choice"},
    }, error_response.text
    M.upstream.stream_chat = fake_stream


# 客户端在首个 SSE 事件后取消，也必须关闭上游、释放账号并记录取消。
cancel_state = {"upstream_closed": False}
release_calls: list[dict[str, Any]] = []
log_calls: list[dict[str, Any]] = []
original_release = M.pool.release
original_log_call = M._log_call


def cancellable_stream(token: str, payload: dict[str, Any], proxy: str | None = None,
                       timeout: float = 300.0):
    try:
        yield {"choices": [{"index": 0, "delta": {"role": "assistant"},
                            "finish_reason": None}]}
        yield {"choices": [{"index": 0, "delta": {"content": "不应被读取"},
                            "finish_reason": None}]}
    finally:
        cancel_state["upstream_closed"] = True


def release_spy(acc: Account, *args: Any, **kwargs: Any) -> None:
    release_calls.append(kwargs)


def log_spy(request: Request, **kwargs: Any) -> None:
    log_calls.append(kwargs)


async def cancel_after_message_start() -> None:
    request_body = json.dumps({
        "model": "hy3", "stream": True, "max_tokens": 32,
        "messages": [{"role": "user", "content": "test"}],
    }).encode()
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": request_body, "more_body": False}

    request = Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/v1/messages",
        "raw_path": b"/v1/messages", "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
    }, receive)
    response = await M.anthropic_messages(request)
    first_event = await response.body_iterator.__anext__()
    assert "message_start" in first_event
    await response.body_iterator.aclose()


try:
    M.upstream.stream_chat = cancellable_stream
    M.pool.release = release_spy
    M._log_call = log_spy
    asyncio.run(cancel_after_message_start())
finally:
    M.upstream.stream_chat = ORIGINAL_STREAM_CHAT
    M.pool.release = original_release
    M._log_call = original_log_call

assert cancel_state["upstream_closed"] is True, cancel_state
assert len(release_calls) == 1 and not release_calls[0].get("error"), release_calls
assert len(log_calls) == 1 and log_calls[0].get("ok") is False, log_calls
assert log_calls[0].get("error") == "client_disconnected", log_calls

print("OK: Anthropic 工具、多模态结果、错误格式、两轮续接及流式取消")
