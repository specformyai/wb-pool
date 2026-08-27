#!/usr/bin/env python3
"""冷启动冒烟：全新环境、空 data 目录、无 .env，服务必须能起来。

这是 CI 里最接近「别人 clone 下来第一次跑」的一步。它要抓的不是逻辑 bug，
而是「开源项目在干净环境下压根起不来」这类问题：

  * 数据目录不存在时能不能自建
  * 没有任何 token / key 时会不会启动即崩
  * 默认出口表为空时代理层会不会炸（原来 DEFAULT_EXITS 写死了私有端口）
  * 首启创建的默认管理员是否触发强制改密闸门
  * /login 与 / 的跳转关系是否正确

不打真实上游：不配 uoomsg token、余额刷新关掉、签到 cron 清空。

    python tests/smoke_cold_start.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(tempfile.mkdtemp(prefix="wbsmoke-"))

# 干净环境：连 data 目录都让它自己建（先删掉 mkdtemp 建的那个）
shutil.rmtree(DATA, ignore_errors=True)

os.environ.update({
    "WB_DATA_DIR": str(DATA),
    "WB_API_KEY": "",
    "WB_ADMIN_KEY": "",
    "WB_UOOMSG_TOKEN": "",
    "WB_PROXY_MODE": "off",
    "WB_EXITS": "",
    "WB_CHECKIN_CRON": "",
    "WB_BALANCE_INTERVAL_MIN": "9999",
})
sys.path.insert(0, str(ROOT))

_p = _f = 0


def check(cond, name, extra: object = ""):
    global _p, _f
    if cond:
        _p += 1
        print(f"  PASS  {name}")
    else:
        _f += 1
        print(f"  FAIL  {name}" + (f"  [{extra}]" if extra != "" else ""))


print("=== 1) 干净环境能 import 并建起 app ===")
try:
    from fastapi.testclient import TestClient

    from app.main import app
    check(True, "app 能 import（无 token/key/出口表也不崩）")
except Exception as exc:  # noqa: BLE001
    check(False, "app 能 import", repr(exc))
    print(f"\n{_p} passed, {_f} failed")
    sys.exit(1)

check(DATA.exists(), "数据目录被自动创建", str(DATA))

print("\n=== 2) 起 TestClient（跑 startup 钩子：调度器 + 模型清单）===")
with TestClient(app) as c:
    r = c.get("/api/health")
    check(r.status_code == 200, "health 200", r.status_code)
    body = r.json() if r.status_code == 200 else {}
    check(body.get("ok") is True, "health ok=true", body)
    check(body.get("authed") is False, "未登录不泄露池子统计", body)

    print("\n=== 3) 首启的强制改密闸门 ===")
    st = c.get("/api/auth/state")
    check(st.status_code == 200, "auth/state 免登录可访问", st.status_code)
    sd = st.json() if st.status_code == 200 else {}
    check(sd.get("default_password") is True, "首启就是默认密码", sd)
    check(sd.get("must_change_password") is True, "并且要求强制改密", sd)
    check(c.get("/api/pool").status_code == 403, "默认密码下业务接口被闸门拦住")

    print("\n=== 4) 入口页与登录页 ===")
    r = c.get("/", follow_redirects=False)
    check(r.status_code == 302, "未登录访问 / 会 302", r.status_code)
    check(r.headers.get("location", "").endswith("/login"),
          "302 目标是 /login", r.headers.get("location"))
    r = c.get("/login")
    check(r.status_code == 200, "/login 200", r.status_code)
    check("no-store" in (r.headers.get("cache-control") or ""),
          "/login 带 no-store（否则拿不到新的资源哈希）",
          r.headers.get("cache-control"))

    print("\n=== 5) 改掉默认密码后功能解锁 ===")
    check(c.post("/api/auth/login",
                 json={"user": "admin", "password": "admin"}).status_code == 200,
          "默认账号可登录")
    check(c.post("/api/auth/password",
                 json={"old": "admin", "new": "smoke-pass-123"}).status_code == 200,
          "改密成功")
    # change_password 里会 revoke_user()，把该用户所有 session 一起踢掉（含当前这条）。
    # 这是对的：改密之后旧凭据签发的会话不该继续有效。所以这里必须重新登录，
    # 顺手把这个安全行为也断言下来。
    check(c.get("/api/pool").status_code == 401, "改密后旧 session 被踢掉")
    check(c.post("/api/auth/login",
                 json={"user": "admin", "password": "smoke-pass-123"}).status_code == 200,
          "用新密码重新登录")
    check(c.get("/api/pool").status_code == 200, "改密后业务接口解锁")

    print("\n=== 6) 空出口表下代理层不炸 ===")
    r = c.get("/api/proxy")
    check(r.status_code == 200, "/api/proxy 200", r.status_code)
    pd = r.json() if r.status_code == 200 else {}
    check(pd.get("mode") == "off", "默认直连模式", pd.get("mode"))
    check(pd.get("exits") == [], "出口表默认为空（没有写死的私有端口）", pd.get("exits"))
    check(isinstance(pd.get("example_exits"), (list, dict)),
          "但保留了示例出口供参考", type(pd.get("example_exits")).__name__)

    print("\n=== 7) 运行时配置层可读写 ===")
    r = c.get("/api/settings")
    check(r.status_code == 200, "/api/settings 200", r.status_code)
    sd = r.json() if r.status_code == 200 else {}
    check(len(sd.get("spec") or []) >= 10, "配置 schema 有内容",
          len(sd.get("spec") or []))
    txt = r.text
    check("smoke-pass-123" not in txt, "配置接口不回传密码明文")

    # body 形状是 {"settings": {...}}，裸键值对也接受（api_settings_set 两种都吃）。
    # 不是 {"values": ...} —— 那会被当成一个名叫 values 的未知配置项而 400。
    r = c.post("/api/settings", json={"settings": {"balance_interval_min": 17}})
    check(r.status_code == 200, "写配置 200", r.text[:120])
    v = c.get("/api/settings").json()["settings"]
    check(v.get("balance_interval_min") == 17, "配置真的存下来了", v.get("balance_interval_min"))
    check((DATA / "settings.json").exists(), "落盘到 data/settings.json")

    print("\n=== 8) 模型清单永不为空（三层降级）===")
    r = c.get("/v1/models")
    check(r.status_code == 200, "/v1/models 200（一把 key 都没有时放行）", r.status_code)
    md = r.json() if r.status_code == 200 else {}
    check(len(md.get("data") or []) > 0, "静态兜底表生效，清单非空",
          len(md.get("data") or []))

print(f"\n{_p} passed, {_f} failed")
shutil.rmtree(DATA, ignore_errors=True)
sys.exit(1 if _f else 0)
