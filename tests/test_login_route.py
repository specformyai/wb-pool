#!/usr/bin/env python3
"""独立 /login 路由 + 强制改默认密码闸门 —— 真发请求，不打上游。

覆盖：
  1. 未登录访问 / → 302 /login；/login 直接给页面
  2. 默认密码下除白名单外全部 403，白名单（登录/改密/静态/health）能通
  3. WB_ADMIN_KEY 不被这道闸门挡住（脚本运维要能继续用）
  4. 改密后闸门自动解除，功能恢复
  5. 逃生口：任何时候都能「默认密码登录 → 改密 → 恢复」

跑法（线上 venv 没装 pytest）：
    .venv/bin/python tests/test_login_route.py
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATA = tempfile.mkdtemp(prefix="wblogin-")
ADMIN_KEY = "root-key-for-scripts"
os.environ["WB_DATA_DIR"] = DATA
os.environ["WB_ADMIN_KEY"] = ADMIN_KEY
os.environ["WB_BALANCE_INTERVAL_MIN"] = "9999"   # 别让余额刷新真跑

from fastapi.testclient import TestClient  # noqa: E402

import app.main as M  # noqa: E402

_pass = _fail = 0


def check(cond: object, label: str, extra: object = "") -> None:
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  PASS  {label}")
    else:
        _fail += 1
        print(f"  FAIL  {label}  [{extra}]")


# follow_redirects=False：要验的正是重定向本身，跟着跳就看不到 302 了
c = TestClient(M.app, follow_redirects=False)

print("=== 1) 默认密码状态下的闸门 ===")
check(M.webauth.uses_default_password() is True, "首次部署确实是默认密码")

r = c.get("/api/pool")
check(r.status_code == 403, "默认密码下业务接口 403", r.status_code)
check(r.json().get("need_password_change") is True,
      "403 里带 need_password_change 让前端能识别", r.text[:120])

r = c.get("/api/health")
check(r.status_code == 200, "存活探针不受闸门影响（反代/监控要用）", r.status_code)

r = c.get("/api/auth/state")
check(r.status_code == 200, "auth/state 放行（前端要靠它判断状态）", r.status_code)
check(r.json().get("must_change_password") is True,
      "auth/state 告诉前端必须改密", r.text[:160])

print("\n=== 2) ADMIN_KEY 不被闸门挡住 ===")
r = c.get("/api/pool", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
check(r.status_code == 200, "带对的 WB_ADMIN_KEY 照常放行（脚本运维不该被锁）",
      r.status_code)
r = c.get("/api/pool", headers={"Authorization": "Bearer wrong-key"})
check(r.status_code == 403, "错的 key 仍被闸门拦住", r.status_code)

print("\n=== 3) 未登录的页面跳转 ===")
r = c.get("/")
check(r.status_code == 302, "未登录访问 / 是 302 不是 200", r.status_code)
check(r.headers.get("location") == "/login", "跳到 /login",
      r.headers.get("location"))

r = c.get("/login")
check(r.status_code == 200, "/login 直接给页面", r.status_code)
check(r.headers.get("cache-control") == "no-store",
      "登录页 no-store（否则拿到旧 HTML 就看不到新资源哈希）",
      r.headers.get("cache-control"))

print("\n=== 4) 逃生口：默认密码登录 → 改密 → 恢复 ===")
r = c.post("/api/auth/login", json={"user": "admin", "password": "admin"})
check(r.status_code == 200, "能用默认账号登录（否则用户会把自己锁死）", r.text[:120])
check(r.json().get("default_password") is True, "登录响应提示仍是默认密码")

# 登录了但还没改密 —— 业务接口仍该被挡，这是「强制」的含义
r = c.get("/api/pool")
check(r.status_code == 403, "光登录不改密，业务接口依然 403", r.status_code)

# 已登录 + 默认密码时访问 /login 不该被弹回 /，否则改密表单没处放
r = c.get("/login")
check(r.status_code == 200, "默认密码期间 /login 仍可访问（要在这儿改密）",
      r.status_code)

r = c.post("/api/auth/password",
           json={"old_password": "admin", "new_password": "brand-new-pw-9"})
check(r.status_code == 200, "改密成功（body 键用 old_password/new_password）",
      r.text[:200])
check(r.json().get("redirect") == "/login", "改密后给出跳转目标", r.text[:160])
check(M.webauth.uses_default_password() is False, "默认密码标记已清除")

print("\n=== 5) 改密后闸门自动解除 ===")
r = c.get("/api/pool")
check(r.status_code in (200, 401), "闸门已放开（401 是因为改密踢了 session）",
      r.status_code)

r = c.post("/api/auth/login", json={"user": "admin", "password": "brand-new-pw-9"})
check(r.status_code == 200, "用新密码重新登录", r.text[:120])
r = c.get("/api/pool")
check(r.status_code == 200, "业务接口恢复可用", r.status_code)

r = c.get("/")
check(r.status_code == 200, "已登录访问 / 直接给主界面，不再跳转", r.status_code)
r = c.get("/login")
check(r.status_code == 302 and r.headers.get("location") == "/",
      "已登录访问 /login 被送回主界面",
      (r.status_code, r.headers.get("location")))

print("\n=== 6) 旧 body 键仍兼容（不破已有脚本）===")
r = c.post("/api/auth/password", json={"old": "brand-new-pw-9", "new": "second-pw-77"})
check(r.status_code == 200, "old/new 这套旧键也能改密", r.text[:160])

shutil.rmtree(DATA, ignore_errors=True)
print(f"\n{_pass} passed, {_fail} failed")
sys.exit(1 if _fail else 0)
