"""
出口代理管理（HTTP 正向代理池）
==============================
本模块不假设你用哪套代理软件（gost / resin / squid / 自建都行），也不内置任何
私有拓扑：**默认出口表是空的**，出口由使用者在面板上添加，或用 autodiscover()
扫本机端口自动发现。

设计约束（踩过的坑）：

* 出口表必须能运行时改。早期版本把作者自己的 21 个端口写死在 DEFAULT_EXITS 里，
  别人部署后 pick() 会一直去连一堆不存在的本地端口，探活全红且无从下手。
  现在那张表降级为 EXAMPLE_EXITS，仅作文档示例，不参与默认注入。
* 「端口在监听」不等于「这个出口能用」。必须打**真实业务端点**探活 ——
  通用探针（ipify 之类）绿灯 ≠ 上游 API 可达（中间可能被墙/被上游拉黑）。
* 探活结果落盘缓存，进程重启不用重新扫一遍。
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import upstream

# 出口表默认为空 —— 不预置任何私有拓扑。
# 出口来源有三条，优先级由 app/settings.py 决定：面板写入 > WB_EXITS 环境变量 > 空。
DEFAULT_EXITS: dict[int, str] = {}

# 仅作文档/示例用：一套 gost 多国出口长什么样。**不会被自动加载**。
# 想快速起步可以在面板上「导入示例」，或设 WB_EXITS="61001:RO,61002:US,..."。
EXAMPLE_EXITS: dict[int, str] = {
    61001: "RO", 61002: "US", 61003: "GB", 61004: "NL", 61005: "FR", 61006: "SG",
    61007: "HK", 61008: "DE", 61009: "FI", 61010: "JP", 61011: "RU", 61012: "CO",
    61013: "PL", 61014: "TW", 61015: "IT", 61016: "BR",
    60001: "US-s1", 60002: "US-s2", 60003: "US-s3", 60004: "US-s4", 60005: "US-s5",
}

# autodiscover() 默认扫这些区间。选择依据：常见自建代理落在 1080/3128/8080/8118
# 以及 gost 惯用的 6xxxx 段。区间可以自己传，别把 1-65535 全扫（慢且吵）。
DISCOVER_RANGES: tuple[tuple[int, int], ...] = (
    (1080, 1090), (3128, 3130), (8080, 8082), (8118, 8118),
    (60001, 60020), (61001, 61020),
)

# 一次扫描的端口数上限。没有这道闸门时 autodiscover(ranges=((1, 70000),))
# 会真的去 create_connection 七万次（实测扫完要几分钟、开 64 线程狂敲本机），
# 面板上一个手滑的输入就能把自己的机器打瘫 —— 开源后这是必须堵死的口子。
MAX_DISCOVER_PORTS = 4096

PROBE_TTL = 900.0     # 探活结果缓存 15 分钟
BAD_COOLDOWN = 600.0  # 出口 CONNECT 失败后拉黑 10 分钟

# gost 端口在监听 ≠ 上游 resin 节点还活着。节点掉线后 gost 会对 CONNECT
# 直接回 503，httpx 抛 ProxyError("503 Service Unavailable")。
# 这类错误属于链路故障，必须换出口重试，不能记成账号错误。
_PROXY_ERR_HINTS = (
    "503 service unavailable", "502 bad gateway", "504 gateway",
    "proxyerror", "connect tunnel failed", "unable to connect to proxy",
    "connection refused", "all attempts to connect to proxy",
    "cannot connect to proxy",
    # ---- 链路中断类（2026-08-23 补） ----
    # resin 节点掉线/限速时，TLS 握手会在中途被切断，httpx/ssl 抛出的是
    # SSL 层错误而不是 ProxyError。旧实现漏了这一类，于是把出口故障
    # 记进 acc.last_error，导致余额正常的号也挂着 "balance: [SSL: ...]"
    # 假报错（实测 8 个号中招，含 active 且余额 199.99 的 4225）。
    "unexpected_eof_while_reading", "eof occurred in violation of protocol",
    "handshake operation timed out", "sslerror", "ssl: ",
    "_ssl.c", "wrong_version_number", "decryption_failed",
    "bad record mac", "tlsv1", "record layer failure",
    # 读写超时同样是链路问题，不是账号问题
    "read operation timed out", "readtimeout", "connecttimeout",
    "writetimeout", "pooltimeout", "timed out",
    "server disconnected", "connection reset", "remotedisconnected",
    "connection aborted", "incompleteread",
)


def is_proxy_error(msg: object) -> bool:
    """判断一个错误串是否为出口链路故障（而非账号/业务错误）"""
    low = str(msg or "").lower()
    return any(h in low for h in _PROXY_ERR_HINTS)


class ProxyManager:
    """
    模式：
      off      不使用代理，直连
      fixed    固定使用 PROXY_URL
      rotate   在 resin 出口里轮换（每次请求随机挑一个探活通过的）
    """

    def __init__(self, mode: str = "off", host: str = "127.0.0.1",
                 fixed_url: str = "", exits: dict[int, str] | None = None,
                 state_file: str | Path | None = None,
                 on_change: "Callable[[dict[int, str]], None] | None" = None):
        self.mode = mode
        self.host = host
        self.fixed_url = fixed_url
        # exits 为 None 时是空表（DEFAULT_EXITS 本身已是空 dict）——
        # 绝不在这里塞示例拓扑，否则别人部署后会去连一堆不存在的端口。
        self.exits = dict(exits) if exits else dict(DEFAULT_EXITS)
        # 出口表变更时回调（main.py 用它写 settings.json 持久化）
        self.on_change = on_change
        self.state_file = Path(state_file) if state_file else None
        self._lock = threading.RLock()
        self._probe: dict[int, dict[str, Any]] = {}
        self._probed_at = 0.0
        self._rr = 0
        # 后台重探单线程守卫：pick() 每次调用都可能发现 TTL 过期，
        # 没有这个标志会 spawn 一堆并发探活线程（每个再开 8 个 worker）
        self._probing = False
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

    # ---------- 出口表运行时增删（面板用） ----------
    def _notify(self) -> None:
        """出口表变了就回调，让上层持久化。回调异常不能影响内存态。"""
        if not self.on_change:
            return
        try:
            self.on_change(dict(self.exits))
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _valid_port(port: object) -> int:
        p = int(port)
        if not (1 <= p <= 65535):
            raise ValueError(f"端口超出范围: {p}")
        return p

    def add_exit(self, port: int, label: str = "") -> dict[str, Any]:
        """加一个出口。已存在则更新标签（幂等，面板重复提交不报错）。"""
        p = self._valid_port(port)
        with self._lock:
            existed = p in self.exits
            self.exits[p] = (label or "").strip() or f":{p}"
        self._notify()
        return {"ok": True, "port": p, "label": self.exits[p],
                "action": "updated" if existed else "added"}

    def remove_exit(self, port: int) -> dict[str, Any]:
        """删一个出口，连它的探活记录一起清掉（否则前端仍会渲染这张卡）。"""
        p = self._valid_port(port)
        with self._lock:
            if p not in self.exits:
                return {"ok": False, "error": f"出口 {p} 不在表里"}
            self.exits.pop(p, None)
            self._probe.pop(p, None)   # 关键：探活记录也要清
        self._save_state()
        self._notify()
        return {"ok": True, "port": p, "action": "removed"}

    def set_exits(self, exits: dict[int, str]) -> dict[str, Any]:
        """整表替换。删掉的出口同样要清探活记录。"""
        clean = {self._valid_port(k): (str(v).strip() or f":{k}")
                 for k, v in (exits or {}).items()}
        with self._lock:
            gone = set(self._probe) - set(clean)
            for p in gone:
                self._probe.pop(p, None)
            self.exits = clean
        self._save_state()
        self._notify()
        return {"ok": True, "count": len(clean)}

    # ---------- 自动发现 ----------
    def autodiscover(self, ranges: "tuple[tuple[int, int], ...] | None" = None,
                     host: str | None = None, add: bool = False,
                     connect_timeout: float = 0.35) -> dict[str, Any]:
        """扫端口找可用的 HTTP 代理出口。

        两段式，缺一不可：
          ① TCP 连得上（快，几百毫秒扫完一批）
          ② 真实业务探针通过（慢，但「端口开着」不等于「是能用的代理」——
             可能是别的服务，也可能是连得上但出不去的死代理）

        add=False 时只报告不落表，让用户在面板上挑；add=True 直接全部加入。
        """
        import concurrent.futures as cf
        import socket

        h = host or self.host
        rgs = ranges or DISCOVER_RANGES

        # 区间必须逐个校验，不能直接 range(lo, hi+1)：
        #   * (1, 70000)   → 会真去连七万次，把本机打瘫（见 MAX_DISCOVER_PORTS）
        #   * (9000, 8000) → start>end，range 静默给空集，用户以为「扫过了没找到」
        #   * (0, 10) / ("a", 5) → 非法端口/类型，socket 层才报错，报得很晚且难懂
        clean: list[tuple[int, int]] = []
        for item in rgs:
            try:
                lo, hi = int(item[0]), int(item[1])
            except (TypeError, ValueError, IndexError, KeyError):
                return {"ok": False,
                        "error": f"区间格式非法: {item!r}（应为 (start, end) 两个整数）"}
            if not (1 <= lo <= 65535) or not (1 <= hi <= 65535):
                return {"ok": False, "error": f"端口超出 1-65535: ({lo}, {hi})"}
            if lo > hi:
                return {"ok": False, "error": f"区间起点大于终点: ({lo}, {hi})"}
            clean.append((lo, hi))

        candidates = sorted({p for lo, hi in clean for p in range(lo, hi + 1)})
        if len(candidates) > MAX_DISCOVER_PORTS:
            return {"ok": False,
                    "error": f"一次最多扫 {MAX_DISCOVER_PORTS} 个端口，"
                             f"当前 {len(candidates)} 个，请缩小区间"}

        def tcp_open(port: int) -> bool:
            try:
                with socket.create_connection((h, port), timeout=connect_timeout):
                    return True
            except OSError:
                return False

        with cf.ThreadPoolExecutor(64) as ex:
            open_ports = [p for p, ok in
                          zip(candidates, ex.map(tcp_open, candidates)) if ok]

        # 第二段：逐个打真实业务探针。这一步慢，所以只对开着的端口做。
        def verify(port: int) -> dict[str, Any]:
            url = f"http://{h}:{port}"
            t0 = time.monotonic()
            ok, detail = upstream.probe_proxy(url)
            return {"port": port, "ok": ok, "detail": detail,
                    "ms": int((time.monotonic() - t0) * 1000)}

        found: list[dict[str, Any]] = []
        if open_ports:
            with cf.ThreadPoolExecutor(8) as ex:
                found = list(ex.map(verify, open_ports))

        usable = [r for r in found if r["ok"]]
        if add and usable:
            with self._lock:
                for r in usable:
                    self.exits.setdefault(r["port"], f":{r['port']}")
            self._notify()

        return {"ok": True, "host": h,
                "scanned": len(candidates), "tcp_open": open_ports,
                "results": found,
                "usable_ports": [r["port"] for r in usable],
                "added": [r["port"] for r in usable] if add else []}

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
            # 只给业务探针那一跳计时。取 IP 是另一个站点的往返，
            # 算进来会让延迟虚高一倍，看不出出口本身快慢。
            t0 = time.monotonic()
            ok, detail = upstream.probe_proxy(url)
            ms = int((time.monotonic() - t0) * 1000)
            ip = ""
            if ok:
                try:
                    import httpx
                    with httpx.Client(proxy=url, timeout=15) as c:
                        ip = c.get("https://api.ipify.org").text.strip()
                except Exception:  # noqa: BLE001
                    ip = "?"
            return {"port": port, "cc": cc, "ok": ok, "detail": detail,
                    "ip": ip, "ms": ms, "checked_at": time.time()}

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

    def mark_bad(self, proxy_url: str) -> None:
        """出口 CONNECT 失败时主动拉黑，10 分钟后自动解禁。"""
        if not proxy_url:
            return
        # 从 URL 里提取端口
        try:
            port = int(proxy_url.rstrip("/").rsplit(":", 1)[-1])
        except (ValueError, IndexError):
            return
        with self._lock:
            if port in self._probe:
                self._probe[port]["ok"] = False
                self._probe[port]["bad_until"] = time.time() + BAD_COOLDOWN
                self._probe[port]["detail"] = "proxy CONNECT 503 (auto-banned)"
        self._save_state()

    # ---------- 主接口 ----------
    def pick(self) -> str | None:
        """返回本次请求应使用的代理 URL（None = 直连）"""
        if self.mode == "off":
            return None
        if self.mode == "fixed":
            return self.fixed_url or None
        # rotate: 先解禁已过冷却期的出口，再考虑 TTL 重探
        with self._lock:
            now = time.time()
            for p, r in self._probe.items():
                if not r.get("ok") and r.get("bad_until", 0) < now:
                    # 冷却到期：重置为"待探活"，让下次 probe_all 重判
                    r.pop("bad_until", None)
            stale = (now - self._probed_at) > PROBE_TTL
            # 后台重探守卫：已在探就不再 spawn
            should_probe = stale and not self._probing
            if should_probe:
                self._probing = True
        if should_probe:
            def _bg_probe():
                try:
                    self.probe_all(force=True)
                finally:
                    with self._lock:
                        self._probing = False
            threading.Thread(target=_bg_probe, daemon=True).start()
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
        # exits 明细也要给前端：面板要能显示「配置了但还没探测」的出口，
        # 并且删除按钮需要知道有哪些端口。只给数量的话前端只能瞎猜。
        with self._lock:
            exits = [{"port": p, "label": self.exits[p]} for p in sorted(self.exits)]
        return {
            "mode": self.mode,
            "host": self.host,
            "fixed_url": self.fixed_url,
            "exits_configured": len(exits),
            "exits": exits,
            "usable": self.usable_ports(),
            "probed_at": self._probed_at,
            "results": self.probe_results(),
            "example_exits": [{"port": p, "label": l}
                              for p, l in sorted(EXAMPLE_EXITS.items())],
            "discover_ranges": [list(r) for r in DISCOVER_RANGES],
        }
