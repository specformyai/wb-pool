#!/usr/bin/env python3
"""ProxyManager 运行时出口表：手动增删、整表替换、自动探测、空表降级。

不打真实网络：所有探针都 monkeypatch 掉。
autodiscover 的 TCP 扫描用一个真实的本地监听 socket 来做正样本，
这样「端口开着但不是代理」这条判据也能被真实覆盖。

跑法（线上 venv 没装 pytest）：
    .venv/bin/python tests/test_proxy_exits.py
"""
from __future__ import annotations

import pathlib
import socket
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import proxies as P  # noqa: E402
from app.proxies import EXAMPLE_EXITS, ProxyManager  # noqa: E402

_pass = _fail = 0


def check(cond: object, label: str, extra: object = "") -> None:
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  PASS  {label}")
    else:
        _fail += 1
        print(f"  FAIL  {label}  [{extra}]")


def new_pm(**kw) -> ProxyManager:
    d = tempfile.mkdtemp()
    kw.setdefault("state_file", pathlib.Path(d) / "proxy_state.json")
    return ProxyManager(**kw)


# ---------------------------------------------------------------- 1) 默认空表
print("\n=== 1) 默认不内置任何私有拓扑 ===")
check(P.DEFAULT_EXITS == {}, "DEFAULT_EXITS 为空 dict", P.DEFAULT_EXITS)
pm = new_pm()
check(pm.exits == {}, "新实例出口表为空", pm.exits)
check(len(EXAMPLE_EXITS) > 0, "EXAMPLE_EXITS 保留示例供文档引用", len(EXAMPLE_EXITS))
st = pm.status()
check(st["exits_configured"] == 0, "status 里配置出口数为 0", st["exits_configured"])
check(isinstance(st.get("exits"), list), "status 给出 exits 明细数组供前端渲染",
      type(st.get("exits")))

# 空表 + rotate 模式不得崩，且不得凭空返回代理
pm.mode = "rotate"
check(pm.pick() is None, "空表 rotate 模式 pick() 回落直连（不崩）", pm.pick())
pm.mode = "off"
check(pm.pick() is None, "off 模式 pick() 为 None", pm.pick())


# ---------------------------------------------------------------- 2) 手动增删
print("\n=== 2) 手动添加 / 删除 / 整表替换 ===")
pm = new_pm()

r = pm.add_exit(3128, "squid-local")
check(r.get("ok") is True, "add_exit 成功", r)
check(pm.exits == {3128: "squid-local"}, "出口已加入", pm.exits)

# 幂等：重复添加只更新标签，不报错
r = pm.add_exit(3128, "squid-renamed")
check(r.get("ok") is True, "重复 add_exit 不报错（幂等）", r)
check(pm.exits[3128] == "squid-renamed", "标签被更新", pm.exits)

# 非法端口必须拒绝
# 非法端口的契约是「抛 ValueError」，不是返回 {"ok": False}。
# 这样设计是对的：面板层负责把它转成 400，内部调用方不会拿着脏值继续跑。
bad = [0, -1, 70000, 65536, "abc", None]
raised = []
for b in bad:
    try:
        pm.add_exit(b)  # type: ignore[arg-type]
        raised.append(False)
    except (ValueError, TypeError):
        raised.append(True)
check(all(raised), "非法端口全部抛 ValueError/TypeError", raised)
check(all(int(p) not in pm.exits for p in (0, -1, 70000, 65536)),
      "非法端口没有被写进出口表", sorted(pm.exits))
check(pm.exits == {3128: "squid-renamed"}, "非法端口没污染出口表", pm.exits)

# 删除要连探活记录一起清，否则前端还会渲染那张卡
pm.add_exit(8080, "tinyproxy")
pm._probe[8080] = {"port": 8080, "ok": True, "cc": "tinyproxy"}
r = pm.remove_exit(8080)
check(r.get("ok") is True, "remove_exit 成功", r)
check(8080 not in pm.exits, "出口已移除", pm.exits)
check(8080 not in pm._probe, "探活记录一并清除（否则前端仍渲染该卡）", pm._probe)

r = pm.remove_exit(9999)
check(r.get("ok") is False, "删除不存在的出口返回 ok=False", r)

# 整表替换
r = pm.set_exits({1080: "socks-ish", 8118: "privoxy"})
check(r.get("ok") is True, "set_exits 成功", r)
check(pm.exits == {1080: "socks-ish", 8118: "privoxy"}, "整表已替换", pm.exits)
check(3128 not in pm.exits, "旧出口被替换掉", pm.exits)


# ---------------------------------------------------------------- 3) 变更回调
print("\n=== 3) 变更回调（上层据此持久化）===")
seen: list[dict] = []
pm = new_pm(on_change=lambda e: seen.append(dict(e)))
pm.add_exit(3128, "a")
pm.add_exit(8080, "b")
pm.remove_exit(3128)
pm.set_exits({1080: "c"})
check(len(seen) == 4, "四次变更各触发一次回调", len(seen))
check(seen[-1] == {1080: "c"}, "回调带上最新全表", seen[-1] if seen else None)


def boom(_e):
    raise RuntimeError("listener exploded")


pm2 = new_pm(on_change=boom)
try:
    r = pm2.add_exit(3128, "x")
    ok = r.get("ok") is True and pm2.exits == {3128: "x"}
except Exception as exc:  # noqa: BLE001
    ok = False
    r = exc
check(ok, "回调抛异常不影响内存态与返回值", r)


# ---------------------------------------------------------------- 4) 自动探测
print("\n=== 4) autodiscover：两段式判定 ===")

# 起一个真实监听 socket 当「端口开着」的正样本
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(8)
live_port = srv.getsockname()[1]
def _accept_loop() -> None:
    # 收尾时主线程会 srv.close()，此时阻塞在 accept() 的这个线程会抛
    # OSError(EBADF)。不吞掉的话测试末尾会打一段与断言无关的 traceback，
    # 看起来像测试失败。
    try:
        while True:
            conn, _ = srv.accept()
            conn.close()
    except OSError:
        pass


threading.Thread(target=_accept_loop, daemon=True).start()

# 找一个确定没人监听的端口当负样本
tmp = socket.socket()
tmp.bind(("127.0.0.1", 0))
dead_port = tmp.getsockname()[1]
tmp.close()

pm = new_pm()

# 阶段②的业务探针：这里让它全部通过，只验 TCP 扫描那一段
P_orig = P.upstream.probe_proxy
P.upstream.probe_proxy = lambda url, **kw: (True, "ok")  # type: ignore[assignment]
try:
    r = pm.autodiscover(ranges=((live_port, live_port), (dead_port, dead_port)),
                        host="127.0.0.1", add=False)
    # 返回键名以源码为准：usable_ports（两段都过的）/ tcp_open（只过第一段）
    # / results（逐端口明细）。这里没有叫 found 的键。
    usable = r.get("usable_ports", [])
    check(live_port in usable, "扫到真实监听端口", r)
    check(dead_port not in usable, "未监听端口不入选", usable)
    check(dead_port not in r.get("tcp_open", []),
          "未监听端口连 TCP 段都没过", r.get("tcp_open"))
    check(pm.exits == {}, "add=False 时只报告不落库", pm.exits)

    r = pm.autodiscover(ranges=((live_port, live_port),),
                        host="127.0.0.1", add=True)
    check(live_port in pm.exits, "add=True 时写入出口表", pm.exits)
finally:
    P.upstream.probe_proxy = P_orig  # type: ignore[assignment]

# 阶段②否决：端口开着但业务探针不通 —— 不能算可用出口
pm3 = new_pm()
P.upstream.probe_proxy = lambda url, **kw: (False, "not a proxy")  # type: ignore[assignment]
try:
    r = pm3.autodiscover(ranges=((live_port, live_port),),
                         host="127.0.0.1", add=True)
    check(live_port not in r.get("usable_ports", []),
          "端口开着但业务探针不通 → 不算可用出口（两段式生效）", r)
    check(live_port in r.get("tcp_open", []),
          "但它确实过了第一段 TCP 检测（证明否决来自第二段）", r.get("tcp_open"))
    check(pm3.exits == {}, "被否决的端口不写入出口表", pm3.exits)
finally:
    P.upstream.probe_proxy = P_orig  # type: ignore[assignment]
    srv.close()

# 非法区间要拒绝，别去扫 6 万个端口
pm4 = new_pm()
r = pm4.autodiscover(ranges=((1, 70000),), host="127.0.0.1")
check(r.get("ok") is False, "越界区间被拒绝", r)
r = pm4.autodiscover(ranges=((5000, 1000),), host="127.0.0.1")
check(r.get("ok") is False, "start>end 的区间被拒绝", r)


print(f"\n{_pass} passed, {_fail} failed")
sys.exit(1 if _fail else 0)
