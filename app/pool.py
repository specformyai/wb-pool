"""
账号池：LRU 轮询 + token 自动刷新 + 配额/失效状态机 + 原子持久化
存储：单个 JSONL 文件（每行一个账号），热加载。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from . import upstream
from .proxies import is_proxy_error

QUOTA_KEYWORDS = ("quota", "insufficient", "余额", "积分不足", "配额", "exceeded",
                  "资源包", "11003", "11004", "arrears")
AUTH_KEYWORDS = ("unauthorized", "401", "invalid_token", "token expired",
                 "invalid grant", "11140", "forbidden")

EXHAUST_COOLDOWN = 12 * 3600      # 配额耗尽冷却 12h（上游按自然日重置，双次重试窗口）
REFRESH_AHEAD = 3600              # 过期前 1h 主动刷新


@dataclass
class Account:
    phone: str = ""
    uid: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0                    # ms
    credits_total: float = -1.0
    credits_checked_at: float = 0.0
    registered_at: str = ""
    label: str = ""
    # 运行时状态
    status: str = "active"                 # active | exhausted | dead | disabled
    last_used: float = 0.0
    last_error: str = ""
    cooldown_until: float = 0.0
    request_count: int = 0
    token_count: int = 0
    credits_spent: float = 0.0
    last_checkin: str = ""
    note: str = ""

    def usable(self) -> bool:
        if self.status in ("dead", "disabled"):
            return False
        if self.status == "exhausted" and time.time() < self.cooldown_until:
            return False
        return bool(self.access_token)

    def expires_in(self) -> float:
        if not self.expires_at:
            return 0.0
        return self.expires_at / 1000.0 - time.time()

    def masked(self) -> str:
        p = self.phone.lstrip("+")
        if p.startswith("86"):
            p = p[2:]
        return f"{p[:3]}****{p[-4:]}" if len(p) >= 7 else p or self.uid[:8]


class AccountPool:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._accounts: list[Account] = []
        self._mtime = 0.0
        # rotation_mode: "lru" = 轮询（负载均衡），"drain" = 优先耗尽当前账号
        self._state_file = self.path.parent / "pool_state.json"
        self.rotation_mode: str = "lru"
        # 由 main.py 注入的 ProxyManager，用于出口故障时换线重试
        self.proxy_mgr: Any = None
        self._load_state()
        self.load()

    # ---------------- 运行策略持久化 ----------------
    def _load_state(self) -> None:
        try:
            if self._state_file.exists():
                st = json.loads(self._state_file.read_text(encoding="utf-8"))
                m = str(st.get("rotation_mode") or "lru").lower()
                if m in ("lru", "drain"):
                    self.rotation_mode = m
        except Exception:  # noqa: BLE001
            pass

    def _save_state(self) -> None:
        try:
            self._state_file.write_text(
                json.dumps({"rotation_mode": self.rotation_mode}, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass

    def set_rotation_mode(self, mode: str) -> tuple[bool, str]:
        m = (mode or "").lower().strip()
        if m not in ("lru", "drain"):
            return False, f"未知策略 {mode}（只支持 lru / drain）"
        with self._lock:
            self.rotation_mode = m
            self._save_state()
        return True, m

    # ---------------- persistence ----------------
    def load(self) -> None:
        with self._lock:
            accs: list[Account] = []
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not raw.get("access_token"):
                        continue
                    known = {f for f in Account.__dataclass_fields__}
                    accs.append(Account(**{k: v for k, v in raw.items() if k in known}))
                self._mtime = self.path.stat().st_mtime
            self._accounts = accs

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for a in self._accounts:
                    f.write(json.dumps(asdict(a), ensure_ascii=False) + "\n")
            tmp.replace(self.path)
            self._mtime = self.path.stat().st_mtime

    def reload_if_changed(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_mtime > self._mtime + 0.5:
                self.load()
        except OSError:
            pass

    # ---------------- crud ----------------
    def all(self) -> list[Account]:
        with self._lock:
            return list(self._accounts)

    def find(self, key: str) -> Account | None:
        with self._lock:
            for a in self._accounts:
                if key in (a.phone, a.uid) or a.phone.lstrip("+") == key.lstrip("+"):
                    return a
        return None

    def add(self, acc: Account) -> tuple[bool, str]:
        with self._lock:
            existing = self.find(acc.phone) if acc.phone else None
            if existing:
                for k, v in asdict(acc).items():
                    if k in ("access_token", "refresh_token", "expires_at", "uid",
                             "credits_total", "credits_checked_at"):
                        setattr(existing, k, v)
                existing.status = "active"
                existing.last_error = ""
                existing.cooldown_until = 0.0
                self.save()
                return True, "updated"
            self._accounts.append(acc)
            self.save()
            return True, "added"

    def remove(self, key: str) -> bool:
        with self._lock:
            acc = self.find(key)
            if not acc:
                return False
            self._accounts.remove(acc)
            self.save()
            return True

    def set_status(self, key: str, status: str) -> bool:
        with self._lock:
            acc = self.find(key)
            if not acc:
                return False
            acc.status = status
            if status == "active":
                acc.cooldown_until = 0.0
                acc.last_error = ""
            self.save()
            return True

    # ---------------- rotation ----------------
    def acquire(self, proxy: str | None = None,
                mode: str | None = None) -> Account | None:
        """
        取一个可用账号，必要时先刷新 token。

        mode="lru"   轮询：取最久未使用的账号，请求摊到全池（默认）
        mode="drain" 耗尽：优先复用最近用过的那个账号，直到它 exhausted
                     再换下一个。少数账号被打光，其余保持满额。
        """
        self.reload_if_changed()
        mode = (mode or self.rotation_mode or "lru").lower()
        with self._lock:
            cands = [a for a in self._accounts if a.usable()]
            if not cands:
                # 冷却期已过的自动复活
                now = time.time()
                for a in self._accounts:
                    if a.status == "exhausted" and now >= a.cooldown_until:
                        a.status = "active"
                cands = [a for a in self._accounts if a.usable()]
            if not cands:
                return None
            if mode == "drain":
                # 最近用过的排最前；同时把余额少的排前面，先把零头打光。
                # last_used=0（从没用过）排最后，避免每次都拉一个新号进来。
                cands.sort(key=lambda a: (-a.last_used, a.credits_total))
            else:
                cands.sort(key=lambda a: a.last_used)
            acc = cands[0]
            acc.last_used = time.time()
            acc.request_count += 1

        if acc.refresh_token and 0 < acc.expires_in() < REFRESH_AHEAD:
            self.try_refresh(acc, proxy=proxy)
        return acc

    def acquire_specific(self, key: str,
                         proxy: str | None = None) -> tuple[Account | None, str]:
        """指定账号取号（对话调试用）。不做可用性筛选，但会说明状态。"""
        self.reload_if_changed()
        acc = self.find(key)
        if not acc:
            return None, f"账号 {key} 不在池中"
        if not acc.access_token:
            return None, f"账号 {acc.masked()} 没有 access_token"
        with self._lock:
            acc.last_used = time.time()
            acc.request_count += 1
        if acc.refresh_token and 0 < acc.expires_in() < REFRESH_AHEAD:
            self.try_refresh(acc, proxy=proxy)
        return acc, ""

    def try_refresh(self, acc: Account, proxy: str | None = None) -> bool:
        res = upstream.refresh_token(acc.refresh_token, proxy=proxy)
        with self._lock:
            if res.get("error"):
                acc.last_error = f"refresh failed: {res['error']}"[:300]
                # 注意：不能因为 refresh 失败就把账号标 dead。
                # 上游 Keycloak 的 client_id=console 不接受 refresh_token grant，
                # 对**所有**账号都返回 401 unauthorized_client（181 正常号实测同样 401）。
                # access_token 本身有效期约 60 天，判活只看对话/余额路径。
                self.save()
                return False
            acc.access_token = res["access_token"]
            acc.refresh_token = res["refresh_token"]
            acc.expires_at = res["expires_at"]
            acc.last_error = ""
            if acc.status == "dead":
                acc.status = "active"
            self.save()
        return True

    def release(self, acc: Account, error: str | None = None,
                tokens: int = 0, credits: float = 0.0) -> None:
        with self._lock:
            acc.token_count += max(0, tokens)
            acc.credits_spent = round(acc.credits_spent + max(0.0, credits), 4)
            if error:
                low = error.lower()
                acc.last_error = error[:300]
                if any(k in low for k in QUOTA_KEYWORDS):
                    acc.status = "exhausted"
                    acc.cooldown_until = time.time() + EXHAUST_COOLDOWN
                elif any(k in low for k in AUTH_KEYWORDS):
                    acc.status = "dead"
            else:
                acc.last_error = ""
                if acc.status == "exhausted" and time.time() >= acc.cooldown_until:
                    acc.status = "active"
            self.save()

    # ---------------- maintenance ----------------
    def refresh_balances(self, proxy: str | None = None) -> list[dict[str, Any]]:
        out = []
        for acc in self.all():
            if not acc.access_token:
                continue
            bal = upstream.get_balance(acc.access_token, proxy=proxy, retries=2)
            # 代理链路故障：拉黑该出口换一个再试，不污染账号 last_error
            if bal.get("total", -1) < 0 and is_proxy_error(bal.get("error")) \
                    and self.proxy_mgr:
                if proxy:
                    self.proxy_mgr.mark_bad(proxy)
                proxy = self.proxy_mgr.pick()
                bal = upstream.get_balance(acc.access_token, proxy=proxy, retries=2)
            with self._lock:
                if bal.get("total", -1) >= 0:
                    acc.credits_total = bal["total"]
                    acc.credits_checked_at = time.time()
                    if acc.status == "exhausted" and bal["total"] > 1:
                        acc.status = "active"
                        acc.cooldown_until = 0.0
                    # 注册时间以上游为准：腾讯侧体验版套餐的 CreateTime 才是
                    # 账号真实注册时间，本地 add/import 时写的是登录时间，要被覆盖
                    if bal.get("registered_at"):
                        acc.registered_at = bal["registered_at"]
                elif not is_proxy_error(bal.get("error")):
                    acc.last_error = f"balance: {bal.get('error', 'unknown')}"[:300]
            out.append({"phone": acc.phone, "masked": acc.masked(),
                        "total": bal.get("total"), "packages": bal.get("packages", []),
                        "error": bal.get("error")})
        self.save()
        return out

    def checkin_one(self, key: str, proxy: str | None = None,
                    force: bool = False) -> dict[str, Any]:
        """单账号签到。force=True 时忽略 last_checkin 直接打上游。"""
        acc = self.find(key)
        if not acc:
            return {"ok": False, "error": "account not found"}
        if not acc.access_token:
            return {"ok": False, "error": "no access_token"}
        if acc.status in ("dead", "disabled"):
            return {"ok": False, "error": f"account is {acc.status}"}
        today = time.strftime("%Y-%m-%d")
        if acc.last_checkin == today and not force:
            return {"ok": False, "skipped": True, "masked": acc.masked(),
                    "error": "今天已签到"}
        res = upstream.daily_checkin(acc.access_token, proxy=proxy)
        # 代理链路故障：拉黑该出口换一个再试一次，不污染账号 last_error
        if not res.get("ok") and not res.get("already") and \
                is_proxy_error(res.get("error")) and self.proxy_mgr:
            if proxy:
                self.proxy_mgr.mark_bad(proxy)
            proxy = self.proxy_mgr.pick()
            res = upstream.daily_checkin(acc.access_token, proxy=proxy)
        with self._lock:
            if res.get("ok") or res.get("already"):
                # already = 上游说今天已签到，同样要落 last_checkin
                acc.last_checkin = today
                acc.last_error = ""
            elif not is_proxy_error(res.get("error")):
                acc.last_error = f"checkin: {res.get('error')}"[:300]
        self.save()
        # 签到成功后余额会变，只刷这一个账号
        # 注意 get_balance 没有 "ok" 字段，失败时 total = -1
        if res.get("ok"):
            bal = upstream.get_balance(acc.access_token, proxy=proxy, retries=2)
            if bal.get("total", -1) >= 0:
                with self._lock:
                    acc.credits_total = bal["total"]
                    acc.credits_checked_at = time.time()
                self.save()
        return {"masked": acc.masked(), "phone": acc.phone,
                "credits_total": acc.credits_total, **res}

    def checkin_all(self, proxy: str | None = None) -> list[dict[str, Any]]:
        out = []
        today = time.strftime("%Y-%m-%d")
        for acc in self.all():
            if not acc.access_token or acc.status in ("dead", "disabled"):
                continue
            if acc.last_checkin == today:
                out.append({"phone": acc.phone, "masked": acc.masked(),
                            "skipped": True, "reason": "already checked in today"})
                continue
            cur_proxy = proxy
            res = upstream.daily_checkin(acc.access_token, proxy=cur_proxy)
            # 代理链路故障：拉黑该出口换一个再试一次，不污染账号 last_error
            if not res.get("ok") and not res.get("already") and \
                    is_proxy_error(res.get("error")) and self.proxy_mgr:
                if cur_proxy:
                    self.proxy_mgr.mark_bad(cur_proxy)
                cur_proxy = self.proxy_mgr.pick()
                res = upstream.daily_checkin(acc.access_token, proxy=cur_proxy)
            with self._lock:
                if res.get("ok") or res.get("already"):
                    acc.last_checkin = today
                    if res.get("already"):
                        acc.last_error = ""
                elif not is_proxy_error(res.get("error")):
                    # 只有非链路错误才写进账号 last_error
                    acc.last_error = f"checkin: {res.get('error')}"[:300]
            out.append({"phone": acc.phone, "masked": acc.masked(), **res})
        self.save()
        # 签到后余额会变，刷新一次
        self.refresh_balances(proxy=proxy)
        return out

    def stats(self) -> dict[str, Any]:
        accs = self.all()
        by_status: dict[str, int] = {}
        for a in accs:
            by_status[a.status] = by_status.get(a.status, 0) + 1
        return {
            "total": len(accs),
            "usable": sum(1 for a in accs if a.usable()),
            "by_status": by_status,
            "credits_total": round(sum(a.credits_total for a in accs if a.credits_total > 0), 2),
            "credits_spent": round(sum(a.credits_spent for a in accs), 4),
            "requests": sum(a.request_count for a in accs),
            "tokens": sum(a.token_count for a in accs),
        }
