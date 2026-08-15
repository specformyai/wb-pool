"""
WebUI 登录（账号密码 + session cookie）
=====================================
原先 WebUI 直接把 API key 填进 localStorage 当登录态，key 明文躺在浏览器里，
而且反代 key 一旦轮换整个面板就打不开。现在改成：

* 账号密码存 `data/webauth.json`，密码只存 PBKDF2-HMAC-SHA256 摘要（600k 轮，随机盐）
* 登录成功下发 `wb_session` cookie（HttpOnly + SameSite=Lax），服务端内存持有 session 表
* 管理接口只认 session；反代 `/v1/*` 仍然走 API key，两条路互不干扰

首启没有任何账号时，用 `WB_ADMIN_USER` / `WB_ADMIN_PASS` 建首个管理员；
两者都没给就落 admin / admin，并在 `/api/auth/state` 里标 `default_password: true`
让前端强提示改密。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

COOKIE_NAME = "wb_session"
SESSION_TTL = 7 * 24 * 3600          # 7 天
PBKDF2_ROUNDS = 600_000
DEFAULT_USER = "admin"
DEFAULT_PASS = "admin"
# 密码表单必须防撞库：同一用户连续失败到阈值就锁一段时间
MAX_FAILS = 8
LOCK_SECONDS = 300


def hash_password(password: str, salt: bytes | None = None) -> dict[str, Any]:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return {"algo": "pbkdf2_sha256", "rounds": PBKDF2_ROUNDS,
            "salt": salt.hex(), "hash": dk.hex()}


def verify_password(password: str, rec: dict[str, Any]) -> bool:
    try:
        salt = bytes.fromhex(rec["salt"])
        rounds = int(rec.get("rounds") or PBKDF2_ROUNDS)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(dk.hex(), rec["hash"])
    except Exception:  # noqa: BLE001
        return False


class WebAuth:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        # token -> {"user":..., "exp":..., "created":..., "ua":...}
        self._sessions: dict[str, dict[str, Any]] = {}
        # user -> {"n": 失败次数, "until": 锁到什么时候}
        self._fails: dict[str, dict[str, float]] = {}
        self.load()
        self._bootstrap()

    # ---------------- 持久化 ----------------
    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                try:
                    self._data = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    self._data = {}
            else:
                self._data = {}
            self._data.setdefault("users", {})

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def _bootstrap(self) -> None:
        """没有任何账号时建首个管理员。"""
        with self._lock:
            if self._data.get("users"):
                return
            user = (os.environ.get("WB_ADMIN_USER") or DEFAULT_USER).strip() or DEFAULT_USER
            pwd = os.environ.get("WB_ADMIN_PASS") or DEFAULT_PASS
            self._data["users"] = {
                user: {**hash_password(pwd),
                       "created_at": time.time(),
                       "is_default": pwd == DEFAULT_PASS}
            }
            self.save()

    # ---------------- 账号 ----------------
    def users(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"user": u, "created_at": v.get("created_at", 0),
                     "is_default": bool(v.get("is_default"))}
                    for u, v in self._data.get("users", {}).items()]

    def uses_default_password(self) -> bool:
        with self._lock:
            return any(v.get("is_default") for v in self._data.get("users", {}).values())

    def check(self, user: str, password: str) -> bool:
        with self._lock:
            rec = self._data.get("users", {}).get((user or "").strip())
        return bool(rec) and verify_password(password or "", rec)

    def change_password(self, user: str, old: str, new: str) -> tuple[bool, str]:
        if len(new or "") < 6:
            return False, "新密码至少 6 位"
        with self._lock:
            rec = self._data.get("users", {}).get(user)
            if not rec:
                return False, "用户不存在"
            if not verify_password(old or "", rec):
                return False, "原密码不对"
            self._data["users"][user] = {
                **hash_password(new), "created_at": rec.get("created_at", time.time()),
                "is_default": False,
            }
            self.save()
        # 改密后把该用户的其它 session 全踢掉
        self.revoke_user(user)
        return True, "已修改"

    # ---------------- session ----------------
    def _gc(self) -> None:
        now = time.time()
        dead = [t for t, s in self._sessions.items() if s["exp"] < now]
        for t in dead:
            self._sessions.pop(t, None)

    def lock_left(self, user: str) -> int:
        """该用户还要锁多少秒（0 = 没锁）。"""
        with self._lock:
            f = self._fails.get((user or "").strip())
            if not f:
                return 0
            left = f.get("until", 0) - time.time()
            return int(left) if left > 0 else 0

    def login(self, user: str, password: str, ua: str = "") -> str | None:
        user = (user or "").strip()
        if self.lock_left(user):
            return None
        if not self.check(user, password):
            with self._lock:
                f = self._fails.setdefault(user, {"n": 0, "until": 0.0})
                f["n"] += 1
                if f["n"] >= MAX_FAILS:
                    f["until"] = time.time() + LOCK_SECONDS
                    f["n"] = 0
            return None
        tok = secrets.token_urlsafe(32)
        with self._lock:
            self._fails.pop(user, None)
            self._gc()
            self._sessions[tok] = {"user": user, "exp": time.time() + SESSION_TTL,
                                   "created": time.time(), "ua": (ua or "")[:120]}
        return tok

    def session(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        with self._lock:
            s = self._sessions.get(token)
            if not s:
                return None
            if s["exp"] < time.time():
                self._sessions.pop(token, None)
                return None
            # 滑动续期：还剩不到一半有效期就顺手延长
            if s["exp"] - time.time() < SESSION_TTL / 2:
                s["exp"] = time.time() + SESSION_TTL
            return dict(s)

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_user(self, user: str) -> int:
        with self._lock:
            dead = [t for t, s in self._sessions.items() if s["user"] == user]
            for t in dead:
                self._sessions.pop(t, None)
            return len(dead)

    def active_sessions(self) -> int:
        with self._lock:
            self._gc()
            return len(self._sessions)
