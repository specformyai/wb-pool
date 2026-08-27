#!/usr/bin/env python3
"""极简 CDP 客户端：在 headless chrome 里跑 JS 并取回结果。

为什么要自己写
--------------
browser-use 那套 helper 在本环境起不来（chrome-not-running），但 chrome 本身
是好的，CDP 端口也在。直连 CDP 反而更可控 —— 而且验证前端必须靠运行时 DOM
和 console 错误，不能靠截图（Pitfall 21/28 的教训）。

注意 DevTools 监听在 ws://[::1]:9222，是 IPv6-only，用 127.0.0.1 连不上。
"""
from __future__ import annotations

import json
import os
import urllib.request

import websocket   # websocket-client

# headless chrome 常常只监听 IPv6（ws://[::1]:9222），127.0.0.1 连不上，
# 所以默认走 [::1]。环境不同就用 WB_CDP_URL 覆盖。
CDP_HTTP = os.environ.get("WB_CDP_URL", "http://[::1]:9222")


def _targets() -> list[dict]:
    with urllib.request.urlopen(f"{CDP_HTTP}/json", timeout=5) as r:
        return json.load(r)


def _page_ws() -> str:
    for t in _targets():
        if t.get("type") == "page":
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("没有 page 类型的 tab")


class Page:
    def __init__(self, ws_url: str | None = None) -> None:
        self.ws = websocket.create_connection(ws_url or _page_ws(),
                                              timeout=30,
                                              suppress_origin=True)
        self._id = 0

    def send(self, method: str, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def goto(self, url: str) -> None:
        """导航并等 load。带 nocache 参数绕开模块图缓存（Pitfall 27）。"""
        self.send("Page.enable")
        self.send("Page.navigate", url=url)
        # 简单等待：轮询 readyState + 让 module 有时间跑
        import time
        for _ in range(60):
            time.sleep(0.25)
            try:
                if self.js("document.readyState") == "complete":
                    break
            except RuntimeError:
                pass
        time.sleep(1.2)   # ES module 是异步的，readyState 完了它可能还没跑

    def js(self, expr: str):
        r = self.send("Runtime.evaluate", expression=expr,
                      returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            exc = r["exceptionDetails"]
            desc = (exc.get("exception") or {}).get("description") or exc.get("text")
            raise RuntimeError(f"JS 异常: {desc}")
        return (r.get("result") or {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:      # noqa: BLE001
            pass
