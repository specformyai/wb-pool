"""
注册 / 登录：协议级两段式流程
=============================
号码由**用户本人提供**（WebUI 表单填写自己的手机号），不接接码平台。

两段式：
  ① start(phone)          → 建立 Keycloak 会话、请求短信验证码，返回 session_id
  ② finish(session_id, code) → 提交验证码 → 静默授权 → 换 token → 查余额 → 入池

实测到的关键点：
  - state 端点：POST copilot.tencent.com/v2/plugin/auth/state?platform=workbuddy
  - Keycloak realm：copilot，client_id=console
  - 验证码错误时 Keycloak 返回 **200**（不是 302），页面里是未渲染的
    `{{ errorMessage }}` 字面量 —— 这就是「码错了」，不是神秘协议错误
  - token 交换：POST www.codebuddy.cn/console/login/enterprise?state=XXX
  - 注册完立刻查余额会拿到 -1（套餐异步发放），必须 sleep + 重试
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from typing import Any

import httpx

from . import upstream
from .pool import Account
from .upstream import CONSOLE, COPILOT, UA

REDIRECT_URI = "https://www.codebuddy.cn/login/?platform=workbuddy"
SESSION_TTL = 600.0     # 短信验证码 5 分钟有效，会话留 10 分钟


class RegisterSession:
    def __init__(self, phone: str, proxy: str | None):
        self.id = secrets.token_urlsafe(12)
        self.phone = phone
        self.proxy = proxy
        self.created_at = time.time()
        self.state = ""
        self.client = httpx.Client(
            proxy=proxy, timeout=httpx.Timeout(60.0, connect=30.0),
            follow_redirects=True,
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        self.log: list[str] = []

    def note(self, msg: str) -> None:
        self.log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def expired(self) -> bool:
        return time.time() - self.created_at > SESSION_TTL

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


class Registrar:
    def __init__(self, pool, proxy_manager):
        self.pool = pool
        self.pm = proxy_manager
        self._sessions: dict[str, RegisterSession] = {}
        self._lock = threading.RLock()

    # ---------------- 内部清理 ----------------
    def _gc(self) -> None:
        with self._lock:
            for sid in [s for s, v in self._sessions.items() if v.expired()]:
                self._sessions.pop(sid).close()

    @staticmethod
    def normalize_phone(raw: str) -> str:
        p = re.sub(r"[^\d+]", "", raw or "")
        if p.startswith("+"):
            return p
        p = p.lstrip("0")
        if p.startswith("86") and len(p) == 13:
            return "+" + p
        return "+86" + p

    # ---------------- 阶段一：发码 ----------------
    def start(self, phone_raw: str, proxy_override: str | None = None) -> dict[str, Any]:
        self._gc()
        phone = self.normalize_phone(phone_raw)
        if not re.fullmatch(r"\+86\d{11}", phone):
            return {"ok": False, "error": "仅支持中国大陆 +86 手机号（上游对其他国家号码一律 400）"}

        proxy = proxy_override if proxy_override is not None else self.pm.pick()
        sess = RegisterSession(phone, proxy)
        sess.note(f"号码 {phone}，出口 {proxy or '直连'}")

        try:
            # ① state
            r = sess.client.post(f"{COPILOT}/v2/plugin/auth/state?platform=workbuddy",
                                 json={}, headers={"Content-Type": "application/json"})
            j = r.json()
            state = (j.get("data") or {}).get("state")
            if not state:
                sess.close()
                return {"ok": False, "error": f"取 state 失败: {r.status_code} {r.text[:200]}"}
            sess.state = state
            sess.note(f"state={state[:24]}…")

            # ② 进入登录页（建立 cookie）
            sess.client.get(f"{COPILOT}/login?platform=workbuddy&state={state}")

            # ③ Keycloak auth 页
            sess.client.get(
                f"{CONSOLE}/auth/realms/copilot/protocol/openid-connect/auth",
                params={"client_id": "console", "state": state,
                        "redirect_uri": REDIRECT_URI, "response_type": "code",
                        "scope": "openid profile email offline_access"})
            sess.note("Keycloak 会话就绪")

            # ④ 请求短信验证码
            rs = sess.client.get(f"{CONSOLE}/auth/realms/copilot/sms/authentication-code",
                                 params={"phoneNumber": phone})
            sess.note(f"发码请求 → HTTP {rs.status_code}")
            if rs.status_code >= 400:
                sess.close()
                return {"ok": False, "error": f"发码失败 HTTP {rs.status_code}: {rs.text[:200]}",
                        "log": sess.log}
        except Exception as exc:  # noqa: BLE001
            sess.close()
            return {"ok": False, "error": f"发码异常: {exc}"[:300]}

        with self._lock:
            self._sessions[sess.id] = sess
        return {"ok": True, "session_id": sess.id, "phone": phone,
                "expires_in": int(SESSION_TTL), "proxy": proxy or "direct",
                "message": "验证码已发送，请在 5 分钟内提交", "log": sess.log}

    # ---------------- 阶段二：提交验证码 ----------------
    def finish(self, session_id: str, code: str, label: str = "",
               invite_code: str = "") -> dict[str, Any]:
        self._gc()
        with self._lock:
            sess = self._sessions.get(session_id)
        if not sess:
            return {"ok": False, "error": "会话不存在或已过期，请重新发码"}
        code = re.sub(r"\D", "", code or "")
        if not code:
            return {"ok": False, "error": "验证码为空"}

        try:
            # ① 重新拉 auth 页拿 form action（含一次性 execution/tab_id）
            r4 = sess.client.get(
                f"{CONSOLE}/auth/realms/copilot/protocol/openid-connect/auth",
                params={"client_id": "console", "redirect_uri": REDIRECT_URI,
                        "state": sess.state, "response_type": "code",
                        "scope": "openid profile email offline_access"})
            m = re.search(r'action="([^"]+)"', r4.text)
            if not m:
                return {"ok": False, "error": "未找到登录表单 action（页面结构可能已变）",
                        "log": sess.log}
            action = m.group(1).replace("&amp;", "&")
            sess.note("取到表单 action")

            # ② 提交验证码
            r5 = sess.client.post(
                action,
                data={"phoneActivated": "true", "phoneNumber": sess.phone, "code": code},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False)
            sess.note(f"提交验证码 → HTTP {r5.status_code}")

            # 200 且页面含 input-error ⇒ 验证码错误（Keycloak 不渲染占位符）
            if r5.status_code == 200:
                err = re.search(r'id="input-error"[^>]*>\s*([^<]*)', r5.text)
                detail = (err.group(1).strip() if err else "")[:120]
                if "{{" in detail or not detail:
                    detail = "验证码错误或已过期"
                return {"ok": False, "error": f"验证码校验失败：{detail}", "log": sess.log}

            # ③ 静默授权（跟随重定向拿到 console 会话）
            sess.client.get(f"{CONSOLE}/console/accounts")
            sess.note("静默授权完成")

            # ④ 换 token
            r6 = sess.client.post(f"{CONSOLE}/console/login/enterprise?state={sess.state}",
                                  headers={"Content-Type": "application/json"})
            j6 = r6.json()
            tok = j6.get("data") or {}
            access = tok.get("accessToken")
            if not access:
                return {"ok": False,
                        "error": f"换 token 失败: HTTP {r6.status_code} {str(j6)[:220]}",
                        "log": sess.log}
            dec = upstream.decode_jwt(access)
            sess.note(f"拿到 token，uid={dec.get('sub', '')[:12]}…")

            # ⑤ 查余额（套餐异步发放，必须等）
            time.sleep(4)
            bal = upstream.get_balance(access, proxy=sess.proxy, retries=3)
            sess.note(f"余额={bal.get('total')}")

            acc = Account(
                phone=sess.phone,
                uid=dec.get("sub", ""),
                access_token=access,
                refresh_token=tok.get("refreshToken", ""),
                expires_at=int(dec.get("exp", 0)) * 1000,
                credits_total=bal.get("total", -1.0),
                credits_checked_at=time.time() if bal.get("total", -1) >= 0 else 0.0,
                registered_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                label=label or "",
                status="active",
            )
            ok, how = self.pool.add(acc)
            sess.note(f"入池: {how}")

            # ⑥ 绑邀请码（新号填别人的码，邀请人得奖励）
            invite_result = None
            if invite_code.strip():
                from . import invite as invite_mod
                invite_result = invite_mod.bind(access, invite_code, proxy=sess.proxy)
                sess.note("邀请码：" + ("绑定成功" if invite_result.get("ok")
                                     else invite_result.get("error", "失败")))
                if invite_result.get("ok"):
                    time.sleep(3)
                    bal = upstream.get_balance(access, proxy=sess.proxy, retries=2)
                    acc.credits_total = bal.get("total", acc.credits_total)
                    self.pool.save()
                    sess.note(f"绑定后余额={bal.get('total')}")

            result = {
                "ok": True, "action": how, "phone": sess.phone,
                "masked": acc.masked(), "uid": acc.uid,
                "credits": bal.get("total"), "packages": bal.get("packages", []),
                "expires_at": acc.expires_at, "invite": invite_result,
                "log": sess.log,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"登录异常: {exc}"[:300], "log": sess.log}
        finally:
            with self._lock:
                s = self._sessions.pop(session_id, None)
            if s:
                s.close()
        return result

    def sessions(self) -> list[dict[str, Any]]:
        self._gc()
        with self._lock:
            return [{"session_id": s.id, "phone": s.phone,
                     "age": round(time.time() - s.created_at, 1),
                     "proxy": s.proxy or "direct", "log": s.log}
                    for s in self._sessions.values()]
