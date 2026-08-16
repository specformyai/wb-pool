"""
多把 API Key 管理
=================
原先反代只认一个环境变量 `WB_API_KEY`，WebUI 也拿它当登录态。现在拆开：

* WebUI 登录 -> `app.webauth`（账号密码 + session）
* 对外调用   -> 本模块，可以生成任意多把 key，各自命名、单独启停、单独统计用量

存储 `data/apikeys.json`，结构与账号池一致的「文件即真源 + mtime 热重载」，
这样多 worker / 手工改文件都不会读到脏数据。

环境变量 `WB_API_KEY` 仍然有效（标记为 env 来源、不可删），方便老客户端平滑过渡。
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

KEY_PREFIX = "wb-"


def gen_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(24)


def mask_key(k: str) -> str:
    if len(k) <= 12:
        return k[:4] + "…"
    return f"{k[:7]}…{k[-4:]}"


class KeyStore:
    def __init__(self, path: str | Path, env_key: str = ""):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.env_key = (env_key or "").strip()
        self._lock = threading.RLock()
        self._keys: list[dict[str, Any]] = []
        self._mtime = 0.0
        self.load()

    # ---------------- 持久化 ----------------
    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._keys = []
                self._mtime = 0.0
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._keys = data.get("keys") or []
                self._mtime = self.path.stat().st_mtime
            except Exception:  # noqa: BLE001
                self._keys = []

    def _reload_if_changed(self) -> None:
        try:
            m = self.path.stat().st_mtime if self.path.exists() else 0.0
        except OSError:
            return
        if m != self._mtime:
            self.load()

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"keys": self._keys}, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)
            try:
                self._mtime = self.path.stat().st_mtime
            except OSError:
                pass

    # ---------------- 查询 ----------------
    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            self._reload_if_changed()
            return list(self._keys)

    def public_list(self) -> list[dict[str, Any]]:
        """给 WebUI 的列表：key 明文只在创建那一刻返回，之后只给掩码。"""
        out: list[dict[str, Any]] = []
        if self.env_key:
            out.append({
                "id": "env", "name": "WB_API_KEY（环境变量）", "masked": mask_key(self.env_key),
                "enabled": True, "source": "env", "created_at": 0,
                "last_used": 0, "request_count": 0, "tokens": 0, "credits": 0.0,
                "note": "来自 .env，不能在这里删；留着兼容老客户端",
            })
        for k in self.all():
            out.append({
                "id": k["id"], "name": k.get("name") or "未命名",
                "masked": mask_key(k.get("key", "")),
                "enabled": bool(k.get("enabled", True)), "source": "store",
                "created_at": k.get("created_at", 0),
                "last_used": k.get("last_used", 0),
                "request_count": k.get("request_count", 0),
                "tokens": k.get("tokens", 0),
                "credits": round(float(k.get("credits") or 0), 5),
                "note": k.get("note", ""),
            })
        out.sort(key=lambda x: (x["source"] != "env", -(x.get("created_at") or 0)))
        return out

    def find_by_id(self, kid: str) -> dict[str, Any] | None:
        with self._lock:
            self._reload_if_changed()
            return next((k for k in self._keys if k["id"] == kid), None)

    # ---------------- 校验 ----------------
    def verify(self, key: str) -> dict[str, Any] | None:
        """
        返回命中的 key 记录（env key 用 {"id": "env"} 表示），未命中返回 None。
        没有任何 key（既无 env 也无库内启用项）时视为「不校验」，返回 {"id": "open"}。
        """
        key = (key or "").strip()
        keys = self.all()
        enabled = [k for k in keys if k.get("enabled", True) and k.get("key")]
        if not self.env_key and not enabled:
            return {"id": "open", "name": "未设置任何 key（不校验）"}
        if not key:
            return None
        if self.env_key and secrets.compare_digest(key, self.env_key):
            return {"id": "env", "name": "WB_API_KEY"}
        for k in enabled:
            if secrets.compare_digest(key, k["key"]):
                return k
        return None

    def has_any(self) -> bool:
        return bool(self.env_key) or any(k.get("enabled", True) for k in self.all())

    # ---------------- 变更 ----------------
    def create(self, name: str = "", note: str = "") -> dict[str, Any]:
        rec = {
            "id": secrets.token_hex(6),
            "name": (name or "").strip() or f"key-{time.strftime('%m%d-%H%M')}",
            "key": gen_key(),
            "enabled": True,
            "created_at": time.time(),
            "last_used": 0.0,
            "request_count": 0,
            "tokens": 0,
            "credits": 0.0,
            "note": (note or "").strip(),
        }
        with self._lock:
            self._reload_if_changed()
            self._keys.append(rec)
            self.save()
        return rec

    def update(self, kid: str, *, name: str | None = None,
               enabled: bool | None = None, note: str | None = None) -> bool:
        with self._lock:
            self._reload_if_changed()
            k = next((x for x in self._keys if x["id"] == kid), None)
            if not k:
                return False
            if name is not None:
                k["name"] = name.strip() or k["name"]
            if enabled is not None:
                k["enabled"] = bool(enabled)
            if note is not None:
                k["note"] = note.strip()
            self.save()
            return True

    def rotate(self, kid: str) -> str | None:
        """换一把新的明文，id/名字/统计都保留。"""
        with self._lock:
            self._reload_if_changed()
            k = next((x for x in self._keys if x["id"] == kid), None)
            if not k:
                return None
            k["key"] = gen_key()
            self.save()
            return k["key"]

    def delete(self, kid: str) -> bool:
        with self._lock:
            self._reload_if_changed()
            n = len(self._keys)
            self._keys = [x for x in self._keys if x["id"] != kid]
            if len(self._keys) == n:
                return False
            self.save()
            return True

    # ---------------- 用量 ----------------
    def record_use(self, kid: str, tokens: int = 0, credits: float = 0.0) -> None:
        if not kid or kid in ("env", "open"):
            return
        with self._lock:
            self._reload_if_changed()
            k = next((x for x in self._keys if x["id"] == kid), None)
            if not k:
                return
            k["request_count"] = int(k.get("request_count", 0)) + 1
            k["tokens"] = int(k.get("tokens", 0)) + int(tokens or 0)
            k["credits"] = round(float(k.get("credits") or 0) + float(credits or 0), 6)
            k["last_used"] = time.time()
            self.save()
