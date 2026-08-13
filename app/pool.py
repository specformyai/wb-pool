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
        self.load()

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
    def acquire(self, proxy: str | None = None) -> Account | None:
        """取最久未使用的可用账号（LRU），必要时先刷新 token。"""
        self.reload_if_changed()
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
            cands.sort(key=lambda a: a.last_used)
            acc = cands[0]
            acc.last_used = time.time()
            acc.request_count += 1

        if acc.refresh_token and 0 < acc.expires_in() < REFRESH_AHEAD:
            self.try_refresh(acc, proxy=proxy)
        return acc

    def try_refresh(self, acc: Account, proxy: str | None = None) -> bool:
        res = upstream.refresh_token(acc.refresh_token, proxy=proxy)
        with self._lock:
            if res.get("error"):
                acc.last_error = f"refresh failed: {res['error']}"[:300]
                if any(k in res["error"].lower() for k in AUTH_KEYWORDS):
                    acc.status = "dead"
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
            with self._lock:
                if bal.get("total", -1) >= 0:
                    acc.credits_total = bal["total"]
                    acc.credits_checked_at = time.time()
                    if acc.status == "exhausted" and bal["total"] > 1:
                        acc.status = "active"
                        acc.cooldown_until = 0.0
                else:
                    acc.last_error = f"balance: {bal.get('error', 'unknown')}"[:300]
            out.append({"phone": acc.phone, "masked": acc.masked(),
                        "total": bal.get("total"), "packages": bal.get("packages", []),
                        "error": bal.get("error")})
        self.save()
        return out

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
            res = upstream.daily_checkin(acc.access_token, proxy=proxy)
            with self._lock:
                if res.get("ok"):
                    acc.last_checkin = today
                else:
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
