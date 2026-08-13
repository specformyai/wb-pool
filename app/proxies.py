"""
resin/gost 出口代理管理
=======================
resin 在 165 上暴露 16 国 HTTP 出口 61001-61016 + 4 个美国 slot 60001-60004。
所有出口必须打真实业务端点探活 —— 通用探针(ipify)绿灯 ≠ copilot.tencent.com 可用。
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path
from typing import Any

from . import upstream

# 端口 → 国家标签（resin 平台命名，实际落地国以探活时的出口 IP 为准）
DEFAULT_EXITS: dict[int, str] = {
    61001: "RO", 61002: "US", 61003: "GB", 61004: "NL", 61005: "FR", 61006: "SG",
    61007: "HK", 61008: "DE", 61009: "FI", 61010: "JP", 61011: "RU", 61012: "CO",
    61013: "PL", 61014: "TW", 61015: "IT", 61016: "BR",
    60001: "US-s1", 60002: "US-s2", 60003: "US-s3", 60004: "US-s4",
}

PROBE_TTL = 900.0   # 探活结果缓存 15 分钟


class ProxyManager:
    """
    模式：
      off      不使用代理，直连
      fixed    固定使用 PROXY_URL
      rotate   在 resin 出口里轮换（每次请求随机挑一个探活通过的）
    """

    def __init__(self, mode: str = "off", host: str = "127.0.0.1",
                 fixed_url: str = "", exits: dict[int, str] | None = None,
                 state_file: str | Path | None = None):
        self.mode = mode
        self.host = host
        self.fixed_url = fixed_url
        self.exits = dict(exits or DEFAULT_EXITS)
        self.state_file = Path(state_file) if state_file else None
        self._lock = threading.RLock()
        self._probe: dict[int, dict[str, Any]] = {}
        self._probed_at = 0.0
        self._rr = 0
        self._load_state()

    # ---------- state ----------
    def _load_state(self) -> None:
        if self.state_file and self.state_file.exists():
            try:
                d = json.loads(self.state_file.read_text())
                self._probe = {int(k): v for k, v in (d.get("probe") or {}).items()}
                self._probed_at = d.get("probed_at", 0.0)
            except Exception:  # noqa: BLE001
                pass

    def _save_state(self) -> None:
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"probe": self._probe, "probed_at": self._probed_at},
                                      ensure_ascii=False, indent=2))
            tmp.replace(self.state_file)
        except Exception:  # noqa: BLE001
            pass

    # ---------- helpers ----------
    def url_for(self, port: int) -> str:
        return f"http://{self.host}:{port}"

    def probe_all(self, force: bool = False) -> list[dict[str, Any]]:
        """并发探活所有出口。返回列表，含出口 IP 与目标可达性。"""
        import concurrent.futures as cf

        with self._lock:
            fresh = (time.time() - self._probed_at) < PROBE_TTL
            if fresh and not force and self._probe:
                return self.probe_results()

        def one(item: tuple[int, str]) -> dict[str, Any]:
            port, cc = item
            url = self.url_for(port)
            ok, detail = upstream.probe_proxy(url)
            ip = ""
            if ok:
                try:
                    import httpx
                    with httpx.Client(proxy=url, timeout=15) as c:
                        ip = c.get("https://api.ipify.org").text.strip()
                except Exception:  # noqa: BLE001
                    ip = "?"
            return {"port": port, "cc": cc, "ok": ok, "detail": detail,
                    "ip": ip, "checked_at": time.time()}

        results: list[dict[str, Any]] = []
        with cf.ThreadPoolExecutor(8) as ex:
            for r in ex.map(one, sorted(self.exits.items())):
                results.append(r)

        with self._lock:
            for r in results:
                self._probe[r["port"]] = r
            self._probed_at = time.time()
            self._save_state()
        return results

    def probe_results(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._probe[p] for p in sorted(self._probe)]

    def usable_ports(self) -> list[int]:
        with self._lock:
            return [p for p, r in sorted(self._probe.items()) if r.get("ok")]

    # ---------- 主接口 ----------
    def pick(self) -> str | None:
        """返回本次请求应使用的代理 URL（None = 直连）"""
        if self.mode == "off":
            return None
        if self.mode == "fixed":
            return self.fixed_url or None
        # rotate
        ports = self.usable_ports()
        if not ports:
            if (time.time() - self._probed_at) > 60:
                self.probe_all(force=True)
                ports = self.usable_ports()
        if not ports:
            return self.fixed_url or None
        with self._lock:
            self._rr = (self._rr + 1) % len(ports)
            return self.url_for(ports[self._rr])

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "host": self.host,
            "fixed_url": self.fixed_url,
            "exits_configured": len(self.exits),
            "usable": self.usable_ports(),
            "probed_at": self._probed_at,
            "results": self.probe_results(),
        }
