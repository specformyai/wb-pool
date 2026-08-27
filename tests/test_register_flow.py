#!/usr/bin/env python3
"""
不用任何真实手机号，验证注册 + 邀请码整条链路。

做法：起一个假 WorkBuddy（Keycloak 登录页 + console + 余额 + 邀请接口），
把 upstream.COPILOT / upstream.CONSOLE / invite.V1 / invite.V2 指到本地假服务，
然后完整跑：发码 → 填码 → 换 token → 查余额 → 入池 → 绑邀请码 → 重查余额。

覆盖的是**我们这边**的正确性：表单 action 解析、验证码错误分支、
bind 的 camelCase 字段名、绑定失败不能连带丢账号、绑定成功要重查余额。
真实上游只剩「腾讯会不会发码」这一个未知项。
"""
from __future__ import annotations

import os
import sys

# 让 `python tests/xxx.py` 裸跑就能 import app.*，不依赖 PYTHONPATH。
# 与 tests/ 下其他测试的既有写法保持一致。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import base64
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATE: dict = {}
PORT = 0
SERVER: ThreadingHTTPServer | None = None


def reset_state() -> None:
    STATE.clear()
    STATE.update({
        "sms_sent": [],
        "expect_code": "123456",
        "codes_submitted": [],
        "bound": {},
        "remain": 497.56,
        "fail_sms": False,
    })


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")


class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002  静音
        pass

    def _send(self, code: int, body: str, ctype: str = "application/json") -> None:
        raw = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        if raw.startswith("{"):
            try:
                return json.loads(raw)
            except Exception:  # noqa: BLE001
                return {}
        return {k: v[0] for k, v in parse_qs(raw).items()}

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)

        # Keycloak 登录页：必须含 form action（register.py 用正则抓）
        if p.endswith("/protocol/openid-connect/auth"):
            html = ('<html><body><form id="kc-form-login" method="post" '
                    f'action="http://127.0.0.1:{PORT}/auth/realms/copilot/'
                    'login-actions/authenticate?session_code=SC&amp;execution=EX&amp;tab_id=TAB">'
                    '<input name="phoneNumber"><input name="code"></form></body></html>')
            return self._send(200, html, "text/html; charset=utf-8")

        # 请求短信验证码（GET，带 phoneNumber）
        if p.endswith("/sms/authentication-code"):
            if STATE["fail_sms"]:
                return self._send(500, "upstream busy", "text/plain")
            STATE["sms_sent"].append((q.get("phoneNumber") or [""])[0])
            return self._json({"ok": True})

        if p == "/login" or p.startswith("/login"):
            return self._send(200, "<html>login</html>", "text/html")

        if p == "/console/accounts":
            return self._json({"code": 0, "data": {"ok": True}})

        # 邀请
        if p.endswith("/invitation/v2/my-code"):
            return self._json({"code": 0, "data": {"invite_code": "ownercode111111",
                                                   "expires_at": "never"}})
        if p.endswith("/invitation/v2/my-progress"):
            return self._json({"code": 0, "data": {
                "invite_code": "ownercode111111",
                "invite_count": len(STATE["bound"]),
                "invited_users": list(STATE["bound"].values()),
                "total_credits": 0, "valid_invite_count": 0, "base_credits": 0,
                "promotion_credits": 0, "payment_credits": 0,
                "cap_reached": False, "cap_value": 30000}})
        if p.endswith("/invitation/v2/my-rewards"):
            return self._json({"code": 0, "data": {"total_credits": 0, "details": []}})
        if p.endswith("/invitation/v2/invite-records"):
            return self._json({"code": 0, "data": {"friends": [], "total_invited": 0,
                                                   "total_used": 0, "total_credits": 0}})
        if p.endswith("/invitation/my-code"):        # v1
            return self._json({"code": 0, "data": {"inviteCode": "v1code22222222",
                                                   "expiresAt": "never"}})
        return self._json({"code": 404, "msg": f"no route {p}"}, 404)

    # ----------------------------------------------------------------- POST
    def do_POST(self) -> None:
        u = urlparse(self.path)
        p = u.path
        b = self._body()

        # ① state
        if p.endswith("/v2/plugin/auth/state"):
            return self._json({"code": 0, "data": {"state": "state-" + "x" * 28}})

        # ② 提交验证码
        if "login-actions/authenticate" in p:
            STATE["codes_submitted"].append(b.get("code"))
            if b.get("code") != STATE["expect_code"]:
                # 真实上游：验证码错时返回 200 + 未渲染的模板占位符
                html = ('<html><body><span id="input-error">{{ errorMessage }}'
                        '</span></body></html>')
                return self._send(200, html, "text/html; charset=utf-8")
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{PORT}/console/accounts?code=AC")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # ③ 换 token
        if "/console/login/enterprise" in p:
            payload = {"sub": "u-fake-uid", "exp": int(time.time()) + 3600}
            jwt = f"{_b64({'alg': 'RS256'})}.{_b64(payload)}.sig"
            return self._json({"code": 0, "data": {"accessToken": jwt,
                                                   "refreshToken": "refresh-xyz"}})

        # ④ 余额（真实结构：data.Response.Data.Accounts[]）
        if p.endswith("/v2/billing/meter/get-user-resource"):
            rem = STATE["remain"]
            return self._json({"code": 0, "data": {"Response": {"Data": {
                "TotalCount": 1, "TotalDosage": 0,
                "Accounts": [{
                    "PackageName": "体验版",
                    "CycleCapacityRemainPrecise": rem,
                    "CycleCapacity": 600, "CapacityUsed": 600 - rem,
                    "CycleEndTime": "2026-09-13 00:00:00",
                    "CapacityUnit": "credits"}]}}}})

        # ⑤ 绑邀请码
        if p.endswith("/invitation/v2/bind"):
            code = b.get("inviteCode")     # 只认 camelCase
            if not code:
                return self._json({"code": 10001, "msg": "invalid request format: Key: "
                                   "'BindInviteCodeRequest.InviteCode' Error:Field validation "
                                   "for 'InviteCode' failed on the 'required' tag"}, 400)
            if code == "ownercode111111":
                return self._json({"code": 12313, "msg": "cannot use your own invite code"})
            if code == "expiredcode0000":
                return self._json({"code": 12314,
                                   "msg": "invite code is not for current activity"})
            STATE["bound"]["u-fake-uid"] = code
            STATE["remain"] = 597.56       # 绑定奖励 +100，用来验证"重查余额"
            return self._json({"code": 0, "msg": "OK", "data": {"credits": 100}})

        return self._json({"code": 404, "msg": f"no route {p}"}, 404)


def start_server() -> int:
    global SERVER, PORT
    # 必须是 Threading 版：register 用长连接的 sess.client，get_balance/invite 又各开
    # 新的 httpx.Client。单线程 HTTPServer 会守着第一条 keep-alive 连接不放，后面的
    # 请求全部排队 → 整个测试挂死。
    SERVER = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    SERVER.daemon_threads = True
    PORT = SERVER.server_address[1]
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()
    return PORT


class _NoProxy:
    """假 ProxyManager：全程直连"""
    def pick(self):
        return None


class RegisterFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        port = start_server()
        base = f"http://127.0.0.1:{port}"

        from app import invite, register, upstream
        from app.pool import AccountPool

        # 把上游全指到假服务（register 里是 from-import，要一起改）
        upstream.COPILOT = base
        upstream.CONSOLE = base
        register.COPILOT = base
        register.CONSOLE = base
        register.REDIRECT_URI = f"{base}/login/?platform=workbuddy"
        invite.V2 = base + "/activity/workbuddy/invitation/v2"
        invite.V1 = base + "/activity/workbuddy/invitation"

        cls.invite, cls.upstream = invite, upstream
        cls.tmp = tempfile.mkdtemp()
        cls.pool = AccountPool(f"{cls.tmp}/pool.json")
        cls.registrar = register.Registrar(cls.pool, _NoProxy())

    def setUp(self) -> None:
        reset_state()

    # ---------------- 邀请码模块 ----------------
    def test_01_my_code(self):
        self.assertEqual(self.invite.my_code("tok"), "ownercode111111")

    def test_02_overview_shape(self):
        d = self.invite.overview("tok")
        self.assertEqual(d["invite_code"], "ownercode111111")
        self.assertEqual(d["cap_value"], 30000)
        self.assertIn("invite?id=ownercode111111", d["invite_link"])
        self.assertEqual(d["v1_code"], "v1code22222222")

    def test_03_bind_uses_camelcase_field(self):
        """字段名写回 snake_case 的话这条会挂（上游 400）"""
        r = self.invite.bind("tok", "friendcode99999")
        self.assertTrue(r["ok"], r)
        self.assertEqual(STATE["bound"]["u-fake-uid"], "friendcode99999")

    def test_04_bind_self_rejected(self):
        r = self.invite.bind("tok", "ownercode111111")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], 12313)
        self.assertIn("不能用自己的邀请码", r["error"])

    def test_05_bind_expired_code(self):
        r = self.invite.bind("tok", "expiredcode0000")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], 12314)
        self.assertIn("不属于当前活动", r["error"])

    def test_06_bind_empty_code(self):
        r = self.invite.bind("tok", "   ")
        self.assertFalse(r["ok"])
        self.assertIn("不能为空", r["error"])

    def test_07_bind_strips_whitespace(self):
        r = self.invite.bind("tok", "  friendcode99999  ")
        self.assertTrue(r["ok"], r)
        self.assertEqual(STATE["bound"]["u-fake-uid"], "friendcode99999")

    # ---------------- 注册流程 ----------------
    def test_10_send_code(self):
        r = self.registrar.start("13900000000")
        self.assertTrue(r.get("ok"), r)
        self.assertIn("session_id", r)
        self.assertEqual(STATE["sms_sent"][-1], "+8613900000000")

    def test_11_non_cn_number_rejected_locally(self):
        r = self.registrar.start("+1 415 555 0100")
        self.assertFalse(r.get("ok"))
        self.assertIn("+86", r["error"])
        self.assertEqual(STATE["sms_sent"], [], "不该为非大陆号码打上游")

    def test_12_wrong_code_clear_error(self):
        s = self.registrar.start("13900000001")
        r = self.registrar.finish(s["session_id"], "000000")
        self.assertFalse(r["ok"])
        self.assertIn("验证码", r["error"])
        self.assertNotIn("{{", r["error"], "把 Keycloak 模板占位符原样吐给用户了")

    def test_13_full_register_no_invite(self):
        s = self.registrar.start("13900000002")
        r = self.registrar.finish(s["session_id"], "123456", label="测试号")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["credits"], 497.56)
        self.assertIsNone(r["invite"])
        self.assertTrue(self.pool.find("+8613900000002"))

    def test_14_register_with_invite_rechecks_balance(self):
        """核心：绑定成功后必须重查余额（497.56 → 597.56）"""
        s = self.registrar.start("13900000003")
        r = self.registrar.finish(s["session_id"], "123456",
                                  label="带邀请", invite_code="friendcode99999")
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["invite"]["ok"], r["invite"])
        self.assertEqual(STATE["bound"]["u-fake-uid"], "friendcode99999")
        self.assertEqual(r["credits"], 597.56, "绑定后没有重查余额")

    def test_15_bad_invite_still_enters_pool(self):
        """邀请码绑失败不能连带把账号搞丢"""
        s = self.registrar.start("13900000004")
        r = self.registrar.finish(s["session_id"], "123456",
                                  invite_code="ownercode111111")
        self.assertTrue(r["ok"], "邀请码失败不该让整个注册失败")
        self.assertFalse(r["invite"]["ok"])
        self.assertIn("不能用自己的邀请码", r["invite"]["error"])
        self.assertTrue(self.pool.find("+8613900000004"))

    def test_16_expired_session(self):
        r = self.registrar.finish("nope-session-id", "123456")
        self.assertFalse(r["ok"])
        self.assertIn("会话", r["error"])

    def test_17_sms_failure_reported(self):
        STATE["fail_sms"] = True
        r = self.registrar.start("13900000005")
        self.assertFalse(r.get("ok"), "上游发码失败必须如实报错，不能假装成功")

    def test_18_phone_normalisation(self):
        for raw in ("+8613900000006", "8613900000006", "13900000006", "139 0000 0006"):
            reset_state()
            r = self.registrar.start(raw)
            self.assertTrue(r.get("ok"), f"{raw}: {r}")
            self.assertEqual(STATE["sms_sent"][-1], "+8613900000006", f"{raw} 归一化错")

    def test_19_session_reused_after_wrong_code(self):
        """填错一次不该作废会话，用户能接着重填"""
        s = self.registrar.start("13900000007")
        bad = self.registrar.finish(s["session_id"], "999999")
        self.assertFalse(bad["ok"])
        good = self.registrar.finish(s["session_id"], "123456")
        self.assertTrue(good["ok"], f"重填正确验证码失败了: {good}")

    @classmethod
    def tearDownClass(cls) -> None:
        if SERVER:
            SERVER.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
