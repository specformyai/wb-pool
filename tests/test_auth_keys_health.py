#!/usr/bin/env python3
"""
登录 / API Key / 调用日志可用性 / 积分统计 的端到端断言。

全程假上游（monkeypatch upstream.stream_chat），临时 data 目录，不碰真账号池。

    .venv/bin/python tests/test_auth_keys_health.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
TD = tempfile.mkdtemp(prefix="wbtest-")
os.environ.update({
    "WB_DATA_DIR": TD, "WB_API_KEY": "", "WB_ADMIN_KEY": "",
    "WB_PROXY_MODE": "off", "WB_CHECKIN_CRON": "", "WB_BALANCE_INTERVAL_MIN": "9999",
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
from app.main import app

fails = []
def ck(name, cond, extra: object = ""):
    print(("  OK  " if cond else " FAIL ") + name + ("" if cond else f"  <- {extra}"))
    if not cond: fails.append(name)

with TestClient(app) as c:
    print("── 1. 未登录时的鉴权 ──")
    r = c.get("/api/auth/state"); ck("auth/state 免登录可访问", r.status_code == 200, r.text)
    st = r.json(); ck("初始未登录", st["logged_in"] is False, st)
    ck("报告默认密码", st["default_password"] is True, st)
    # 强制改密中间件跑在鉴权依赖之前：默认密码期受保护接口一律 403
    # need_password_change，而不是 401。真正的「未登录 -> 401」在第 3 节
    # 改密后的登出场景里验（那时才是纯鉴权路径）。
    r_pool = c.get("/api/pool")
    ck("默认密码期 /api/pool 被闸门拦成 403", r_pool.status_code == 403, r_pool.text)
    ck("403 里带 need_password_change 标记",
       r_pool.json().get("need_password_change") is True, r_pool.text)
    ck("默认密码期 /api/keys 同样被拦", c.get("/api/keys").status_code == 403)
    h = c.get("/api/health").json(); ck("health 未登录不泄露池子", h.get("authed") is False and "stats" not in h, h)
    ck("首页 200", c.get("/").status_code == 200)

    print("── 2. 登录 ──")
    ck("错密码 401", c.post("/api/auth/login", json={"user":"admin","password":"nope"}).status_code == 401)
    r = c.post("/api/auth/login", json={"user":"admin","password":"admin"})
    ck("默认 admin/admin 登录成功", r.status_code == 200, r.text)
    ck("下发 HttpOnly cookie", "wb_session" in c.cookies, dict(c.cookies))
    # 登录成功也不解闸 —— 闸门看的是「默认密码改没改」，不是「登录没登录」。
    # 这正是它的价值：光有 session 不足以放开接口，必须先把默认密码换掉。
    ck("登录但未改密时 /api/pool 仍被拦", c.get("/api/pool").status_code == 403)
    # /api/health 在闸门白名单里（监控探针不该因为默认密码没改就变红），
    # 所以这里能正常拿到 stats。
    h = c.get("/api/health").json(); ck("登录后 health 带 stats", h.get("authed") and "stats" in h, h)

    print("── 3. 改密码（同时解开强制改密闸门）──")
    # 位置很关键：默认密码未改时中间件会锁住后面所有接口，
    # 所以这一节必须在 API Key / 调用日志那些节之前跑。
    # old=admin 在这个时点仍然有效，正是它要测的场景。
    ck("旧密码错 -> 400", c.post("/api/auth/password", json={"old":"x","new":"newpass123"}).status_code == 400)
    ck("新密码太短 -> 400", c.post("/api/auth/password", json={"old":"admin","new":"123"}).status_code == 400)
    ck("改密成功", c.post("/api/auth/password", json={"old":"admin","new":"newpass123"}).status_code == 200)
    c.cookies.clear()
    ck("旧密码不能再登录", c.post("/api/auth/login", json={"user":"admin","password":"admin"}).status_code == 401)
    r = c.post("/api/auth/login", json={"user":"admin","password":"newpass123"})
    ck("新密码可登录", r.status_code == 200, r.text)
    ck("不再报默认密码", r.json()["default_password"] is False, r.json())
    ck("登出", c.post("/api/auth/logout").status_code == 200)
    # 闸门解开后才能验纯鉴权语义：这时未登录就是 401，不再被 403 遮住。
    ck("登出后 401", c.get("/api/pool").status_code == 401)
    ck("登出后 /api/keys 也 401", c.get("/api/keys").status_code == 401)
    ck("401 不再是闸门的 need_password_change",
       "need_password_change" not in c.get("/api/pool").text)
    c.post("/api/auth/login", json={"user":"admin","password":"newpass123"})

    print("── 4. API Key 生成/校验 ──")
    ck("初始无 key", c.get("/api/keys").json()["keys"] == [], c.get("/api/keys").json())
    # 没有任何 key 时 /v1 应放行（避免把自己锁在外面）
    ck("无 key 时 /v1/models 放行", c.get("/v1/models").status_code == 200)
    r = c.post("/api/keys", json={"name": "cherry"}); ck("生成 key 200", r.status_code == 200, r.text)
    k1 = r.json()["key"]; kid1 = r.json()["id"]
    ck("明文 key 形如 wb-", k1.startswith("wb-") and len(k1) > 20, k1)
    lst = c.get("/api/keys").json()["keys"]
    ck("列表只回掩码", len(lst) == 1 and "…" in lst[0]["masked"] and k1 not in json.dumps(lst), lst)
    ck("有 key 后裸调 /v1 401", c.get("/v1/models").status_code == 401)
    ck("Bearer 正确 key 放行", c.get("/v1/models", headers={"Authorization": f"Bearer {k1}"}).status_code == 200)
    ck("x-api-key 正确 key 放行", c.get("/v1/models", headers={"x-api-key": k1}).status_code == 200)
    ck("错 key 401", c.get("/v1/models", headers={"Authorization": "Bearer wb-wrong"}).status_code == 401)

    r = c.post("/api/keys", json={"name": "phone"}); k2, kid2 = r.json()["key"], r.json()["id"]
    ck("第二把 key 可用", c.get("/v1/models", headers={"x-api-key": k2}).status_code == 200)
    c.post(f"/api/keys/{kid2}/update", json={"enabled": False})
    ck("停用后该 key 401", c.get("/v1/models", headers={"x-api-key": k2}).status_code == 401)
    ck("另一把不受影响", c.get("/v1/models", headers={"x-api-key": k1}).status_code == 200)
    nk = c.post(f"/api/keys/{kid2}/rotate").json()["key"]
    c.post(f"/api/keys/{kid2}/update", json={"enabled": True})
    ck("轮换后新 key 可用", c.get("/v1/models", headers={"x-api-key": nk}).status_code == 200)
    ck("轮换后旧 key 失效", c.get("/v1/models", headers={"x-api-key": k2}).status_code == 401)
    ck("列表不能恢复密钥明文", c.get(f"/api/keys/{kid2}/reveal").status_code == 404)
    ck("删除 key", c.delete(f"/api/keys/{kid2}").status_code == 200)
    ck("删除后 401", c.get("/v1/models", headers={"x-api-key": nk}).status_code == 401)

    print("── 4b. 真实 chat 落账（假上游） ──")
    from app import main as M
    from app import upstream as U

    def fake_stream(token, payload, proxy=None, timeout=300.0):
        if payload.get("model") == "boom-model":
            raise U.UpstreamError(500, 11140, "模型不可用")
        yield {"choices": [{"delta": {"role": "assistant"}}]}
        yield {"choices": [{"delta": {"content": "你好"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}

    from app.pool import Account
    seed_acc = Account(phone="+8613900000001", status="active", credits_total=100.0)
    seed_acc.access_token = "tok"; seed_acc.expires_at = time.time() + 86400
    M.pool._accounts = [seed_acc]
    orig_stream = U.stream_chat
    M.upstream.stream_chat = fake_stream
    hdr = {"Authorization": f"Bearer {k1}"}
    ck("登录 Cookie 可调用 WebUI 调试接口",
       c.post("/api/chat/completions",
              json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]}).status_code == 200)
    ck("/v1 chat 不接受登录 Cookie 代替 API key",
       c.post("/v1/chat/completions",
              json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]}).status_code == 401)
    anon = TestClient(M.app)
    ck("未登录不能调用 WebUI 调试接口",
       anon.post("/api/chat/completions",
                 json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]}).status_code == 401)
    r = c.post("/v1/chat/completions", headers=hdr,
               json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]})
    ck("非流式 chat 200", r.status_code == 200, r.text[:200])
    ck("回内容", r.json()["choices"][0]["message"]["content"] == "你好", r.json())
    ck("回 usage", r.json()["usage"]["total_tokens"] == 10, r.json().get("usage"))
    r = c.post("/v1/chat/completions", headers=hdr,
               json={"model": "glm-5.3", "stream": True,
                     "messages": [{"role": "user", "content": "hi"}]})
    ck("流式 chat 200", r.status_code == 200 and "data:" in r.text, r.text[:150])
    r = c.post("/v1/messages", headers=hdr,
               json={"model": "glm-5.3", "max_tokens": 64,
                     "messages": [{"role": "user", "content": "hi"}]})
    ck("Anthropic /v1/messages 200", r.status_code == 200, r.text[:200])
    r = c.post("/v1/chat/completions", headers=hdr,
               json={"model": "boom-model", "messages": [{"role": "user", "content": "hi"}]})
    ck("上游报错 -> 5xx", r.status_code >= 400, r.status_code)

    seed_acc.status = "active"; seed_acc.last_error = ""
    r = c.post("/api/chat/completions", headers={"X-WB-Force-Account": seed_acc.phone},
               json={"model": "boom-model", "messages": [{"role": "user", "content": "hi"}]})
    ck("指定账号 11140 后标 dead", r.status_code >= 400 and seed_acc.status == "dead",
       (r.status_code, seed_acc.status))

    # 前端早已模块化拆分，聊天调试那段搬到了 web/chat.js —— 老的 web/app.js
    # 不再存在（这条断言曾因此一直是 FileNotFoundError）。
    js = (Path(__file__).resolve().parents[1] / "web" / "chat.js").read_text(encoding="utf-8")
    ck("WebUI 调试走 session 接口且无残留 KEY",
       "fetch('/api/chat/completions'" in js and "if (KEY)" not in js)

    ks = c.get("/api/keys").json()["keys"][0]
    ck("key 请求数已累计", ks["request_count"] >= 3, ks)
    ck("key token 数已累计", ks["tokens"] >= 20, ks)
    ck("key last_used 有值", ks["last_used"] > 0, ks)
    hh = c.get("/api/calls/health?include_known=false").json()
    mm = {x["model"]: x for x in hh["models"]}
    ck("日志记到 glm-5.3", mm.get("glm-5.3", {}).get("ok", 0) >= 3, list(mm))
    ck("日志记到失败模型 boom-model", mm.get("boom-model", {}).get("fail", 0) == 2, mm.get("boom-model"))
    ck("boom-model 状态 bad", mm.get("boom-model", {}).get("state") == "bad", mm.get("boom-model"))
    rc = c.get("/api/calls/recent?limit=10").json()["calls"]
    ck("recent 带 key 名", any(x.get("key_name") == "cherry" for x in rc), rc[:2])
    ck("recent 带账号掩码", any(x.get("account") for x in rc), rc[:2])
    M.upstream.stream_chat = orig_stream
    c.post("/api/calls/reset")

    print("── 5. 调用日志/可用性 ──")
    from app.main import calllog as CLOG
    now = time.time()
    def seed(model, ok, n, ago_step, ms=800, err="", offset=0.0, ttft=0, tps=0.0,
             acct="+861****0001"):
        # 直接写 jsonl 才能造历史时间戳（record() 用的是当前时间）
        with open(CLOG.path, "a", encoding="utf-8") as f:
            for i in range(n):
                f.write(json.dumps({
                    "ts": round(now - offset - ago_step * i, 3), "model": model, "ok": ok,
                    "endpoint": "chat", "stream": False, "ms": ms + i, "tokens": 10,
                    "ttft_ms": ttft, "tps": tps,
                    "credits": 0.001, "account": acct, "key_id": "", "key_name": "",
                    "code": None if ok else 500, "error": err,
                }, ensure_ascii=False) + "\n")
    seed("glm-5.3", True, 6, 3600, ttft=1500, tps=60.0)
    seed("glm-5.3", False, 3, 120, err="upstream 500 内部错误")
    seed("kimi-k3", True, 4, 600, ms=500, ttft=2000, tps=40.0, acct="+861****0002")
    # 累计成功率高（96%）但最近 20 分钟全挂 —— 专门验 tail_state 不被均值糊掉
    seed("sonnet-x", True, 96, 600, ms=700, offset=1800, ttft=900, tps=75.0)
    seed("sonnet-x", False, 4, 300, err="502 upstream timeout")
    d = c.get("/api/calls/health?window_h=24&buckets=24&include_known=false").json()
    ck("总调用数正确", d["total"] == 113, d["total"])
    ck("成功 106 失败 7", (d["ok"], d["fail"]) == (106, 7), (d["ok"], d["fail"]))
    m = {x["model"]: x for x in d["models"]}
    ck("glm-5.3 有 9 条", m["glm-5.3"]["total"] == 9, m["glm-5.3"]["total"])
    ck("glm-5.3 判为不稳/异常", m["glm-5.3"]["state"] in ("degraded", "bad"), m["glm-5.3"]["state"])
    ck("kimi-k3 全成功=ok", m["kimi-k3"]["state"] == "ok", m["kimi-k3"]["state"])
    ck("桶数 24", len(m["glm-5.3"]["buckets"]) == 24, len(m["glm-5.3"]["buckets"]))
    ck("有 idle 桶", any(b["state"] == "idle" for b in m["kimi-k3"]["buckets"]))
    ck("带最近错误", "500" in (m["glm-5.3"]["last_error"] or ""), m["glm-5.3"]["last_error"])
    ck("p50/p95 有值", (m["glm-5.3"]["p50_ms"] or 0) > 0 and (m["glm-5.3"]["p95_ms"] or 0) > 0,
       (m["glm-5.3"]["p50_ms"], m["glm-5.3"]["p95_ms"]))
    # 新增：首字延迟 / 输出速度 / 账号数 / 尾部趋势
    ck("ttft_ms 有值", m["glm-5.3"]["ttft_ms"] == 1500, m["glm-5.3"]["ttft_ms"])
    ck("tps 有值", m["glm-5.3"]["tps"] == 60.0, m["glm-5.3"]["tps"])
    ck("失败不污染 ttft 均值", m["kimi-k3"]["ttft_ms"] == 2000, m["kimi-k3"]["ttft_ms"])
    ck("账号数按窗口内实际去重", (m["glm-5.3"]["accounts"], m["kimi-k3"]["accounts"]) == (1, 1),
       (m["glm-5.3"]["accounts"], m["kimi-k3"]["accounts"]))
    sx = m["sonnet-x"]
    ck("sonnet-x 累计成功率 96%", sx["rate"] == 0.96, sx["rate"])
    ck("sonnet-x 累计判 ok", sx["state"] == "ok", sx["state"])
    ck("sonnet-x 尾部判 bad（不被均值糊掉）", sx["tail_state"] == "bad", sx["tail_state"])
    ck("kimi-k3 尾部不是 bad", m["kimi-k3"]["tail_state"] != "bad", m["kimi-k3"]["tail_state"])
    ck("KPI model_count", d["model_count"] == 3, d["model_count"])
    ck("KPI 异常含尾部连挂的", d["abnormal"] == 2, (d["abnormal"], d["normal"], d["idle"]))
    ck("KPI 正常只剩 kimi-k3", d["normal"] == 1, d["normal"])
    ck("KPI avg_rate 是各模型均值",
       abs(d["avg_rate"] - round((6/9 + 1.0 + 0.96) / 3, 4)) < 0.0002, d["avg_rate"])
    ck("正在挂的排最前", d["models"][0]["model"] in ("glm-5.3", "sonnet-x"),
       [x["model"] for x in d["models"]])
    ck("recent 有数据", len(c.get("/api/calls/recent?limit=5").json()["calls"]) == 5)
    d7 = c.get("/api/calls/health?window_h=168&buckets=28&include_known=false").json()
    ck("7 天窗口 28 桶", len(d7["models"][0]["buckets"]) == 28)
    dk = c.get("/api/calls/health?window_h=24&buckets=24&include_known=true").json()
    ck("include_known 把没调过的模型也列出来(idle)",
       any(x["state"] == "idle" and x["total"] == 0 for x in dk["models"]),
       [(x["model"], x["state"]) for x in dk["models"]][:6])
    ck("reset 清空", c.post("/api/calls/reset").json().get("ok") is True)
    ck("reset 后 total=0", c.get("/api/calls/health?include_known=false").json()["total"] == 0)

    print("── 6. 积分统计排除不可用 ──")
    from app.main import pool as P
    from app.pool import Account
    accs = [
        Account(phone="+8613800000001", status="active",   credits_total=1000.0),
        Account(phone="+8613800000002", status="active",   credits_total=500.0),
        Account(phone="+8613800000003", status="dead",     credits_total=7777.0),
        Account(phone="+8613800000004", status="disabled", credits_total=333.0),
        Account(phone="+8613800000005", status="exhausted",credits_total=0.5),
    ]
    for a in accs:
        a.access_token = "t"; a.expires_at = time.time() + 86400
    P._accounts = accs
    s = P.stats()
    ck("总积分只算可用账号", abs(s["credits_total"] - 1500.5) < 1e-6, s["credits_total"])
    ck("credits_total_all 含全部", abs(s["credits_total_all"] - 9610.5) < 1e-6, s["credits_total_all"])
    ck("被排除积分 = 8110.0", abs(s["credits_unusable"] - 8110.0) < 1e-6, s["credits_unusable"])
    ck("usable 计数", s["usable"] == 3, s)
    pl = c.get("/api/pool").json()
    ck("/api/pool 每行带 usable 标记",
       [a["usable"] for a in pl["accounts"]] == [True, True, False, False, True],
       [(a["masked"], a["status"], a["usable"]) for a in pl["accounts"]])

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
shutil.rmtree(TD, ignore_errors=True)
sys.exit(1 if fails else 0)
