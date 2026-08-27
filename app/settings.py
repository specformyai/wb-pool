"""
运行时配置层（settings.json）
=============================
为什么需要它
------------
本项目原先所有配置都靠 `.env` + 环境变量，进程 import 时固化成模块级常量。
那对「自己部署自己维护」够用，但作为开源项目有两个硬伤：

1. 别人 clone 下来想填一个接码平台 token，得去编辑 `.env` 再重启服务；
   容器里 `.env` 常是只读挂载，根本改不了。
2. 代理出口这类拓扑信息被写死在代码里（`proxies.DEFAULT_EXITS` 曾内置
   21 个 resin 端口 + 国家标签），等于把部署者的私有环境塞进了源码。

所以引入一层可持久化、可热生效的运行时配置：

    优先级：运行时配置(settings.json) > 环境变量 > 代码默认值

读取一律走 `get(key)`，**不要**在模块顶层把值固化成常量 —— 那正是
「前端改完不生效」的根源。写入走 `set_many()`，落盘后同一进程内立即生效。

设计要点
--------
* **环境变量是「初始值」不是「上限」**：首次启动时若 settings.json 里没有某项，
  读取时回落到 env；用户在前端改过之后，settings.json 优先，env 不再影响它。
  这样既兼容老部署（env 照旧生效），又允许前端接管。
* **敏感值不出网**：`public_view()` 把 token/密码类字段脱敏成
  `{"set": true, "hint": "末4位"}`，前端只知道「配没配」，拿不到明文。
  这是给 WebUI 用的；后端自己取值走 `get()`。
* **schema 驱动**：每个键声明类型、env 名、是否敏感。类型转换和校验集中在
  `coerce()`，避免前端传字符串 "10" 进来把 int 配置污染成 str。
* **原子写 + 0600**：先写 .tmp 再 replace，避免并发读到半个文件；
  含 token 的文件权限收紧到只有属主可读。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------------------- #
# schema
#   type: bool | int | str | exits
#   env:  对应的环境变量名（None = 不从 env 读）
#   secret: True 时 public_view 只暴露「是否已设置」
# --------------------------------------------------------------------------- #
SPEC: dict[str, dict[str, Any]] = {
    # ---- 出口代理 ----
    # 默认 off：新部署的人没有代理池，rotate 会让每个请求都白探活一遍。
    "proxy_mode": {"type": "str", "env": "WB_PROXY_MODE", "default": "off",
                   "choices": ("off", "fixed", "rotate")},
    "proxy_host": {"type": "str", "env": "WB_PROXY_HOST", "default": "127.0.0.1"},
    "proxy_url": {"type": "str", "env": "WB_PROXY_URL", "default": ""},
    # 出口表：{端口: 标签}。空 = 没有出口，rotate 会自动退化成直连。
    # 原先这里内置 21 个 resin 端口，那是部署者的私有拓扑，不该进源码。
    "proxy_exits": {"type": "exits", "env": "WB_EXITS", "default": {}},

    # ---- 接码平台（自动注册用；不填则自动注册不可用，其余功能不受影响）----
    "uoomsg_token": {"type": "str", "env": "WB_UOOMSG_TOKEN", "default": "",
                     "secret": True},

    # ---- 调度 ----
    "checkin_cron": {"type": "str", "env": "WB_CHECKIN_CRON", "default": "5 1 * * *"},
    "balance_interval_min": {"type": "int", "env": "WB_BALANCE_INTERVAL_MIN",
                             "default": 10, "min": 1, "max": 1440},

    # ---- 账号可用性 ----
    "verify_below_credits": {"type": "int", "env": "WB_VERIFY_BELOW_CREDITS",
                             "default": 150, "min": 0, "max": 100000},
    "verify_stale_sec": {"type": "int", "env": "WB_VERIFY_STALE_SEC",
                         "default": 120, "min": 0, "max": 86400},
    "auth_fail_limit": {"type": "int", "env": "WB_AUTH_FAIL_LIMIT",
                        "default": 2, "min": 1, "max": 100},
    "expiring_soon_h": {"type": "int", "env": "WB_EXPIRING_SOON_H",
                        "default": 72, "min": 1, "max": 8760},

    # ---- 时区 ----
    # 项目里有十几处 time.strftime("%Y-%m-%d") 按**本地时间**算「今天」，
    # 签到判重（last_checkin == today）和面板「今日签到到账」列都依赖它。
    # 上游是腾讯，业务日以东八区为准，所以默认 Asia/Shanghai；
    # 部署在别的时区的机器如果不设这个，会出现「签到重复跳过」或「今日到账整列为空」。
    "timezone": {"type": "str", "env": "WB_TZ", "default": "Asia/Shanghai"},
}

SECRET_KEYS = frozenset(k for k, v in SPEC.items() if v.get("secret"))


def _parse_exits(raw: Any) -> dict[int, str]:
    """出口表接受三种写法，统一成 {port: label}。

    * dict:  {"61001": "RO", 61002: "US"}
    * str:   "61001:RO,61002:US"  或  "61001,61002"（无标签则标签为空）
    * list:  [{"port": 61001, "label": "RO"}, 61002]

    端口非法（不是 1-65535 的整数）一律丢弃而不是抛异常 —— 配置层不该因为
    一个脏值让整个服务起不来。
    """
    out: dict[int, str] = {}

    def put(port: Any, label: Any = "") -> None:
        try:
            p = int(str(port).strip())
        except (TypeError, ValueError):
            return
        if not (1 <= p <= 65535):
            return
        out[p] = str(label or "").strip()

    if isinstance(raw, dict):
        for k, v in raw.items():
            put(k, v)
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                put(item.get("port"), item.get("label") or item.get("cc") or "")
            else:
                put(item)
    elif isinstance(raw, str):
        for part in raw.replace("\n", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                port, _, label = part.partition(":")
                put(port, label)
            else:
                put(part)
    return out


def coerce(key: str, value: Any) -> Any:
    """按 schema 把外部输入转成内部类型，并做范围/枚举校验。

    抛 ValueError 让调用方回 400，而不是把脏值写进配置文件。
    """
    spec = SPEC.get(key)
    if not spec:
        raise ValueError(f"未知配置项: {key}")
    t = spec["type"]

    if t == "exits":
        return _parse_exits(value)

    if t == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    if t == "int":
        try:
            v = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 必须是整数") from exc
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and v < lo:
            raise ValueError(f"{key} 不能小于 {lo}")
        if hi is not None and v > hi:
            raise ValueError(f"{key} 不能大于 {hi}")
        return v

    v = "" if value is None else str(value).strip()
    choices = spec.get("choices")
    if choices and v not in choices:
        raise ValueError(f"{key} 只能是 {'/'.join(choices)} 之一")
    return v


def _from_env(key: str) -> Any | None:
    spec = SPEC[key]
    env = spec.get("env")
    if not env:
        return None
    raw = os.environ.get(env)
    if raw is None or raw == "":
        return None
    try:
        return coerce(key, raw)
    except ValueError:
        # env 里的脏值不该让服务起不来，退回代码默认值
        return None


class Settings:
    """运行时配置。线程安全，读多写少。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self.load()

    # ---------------- 持久化 ----------------
    def load(self) -> None:
        with self._lock:
            raw: dict[str, Any] = {}
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8")) or {}
                except Exception:  # noqa: BLE001 —— 损坏的配置不能让服务起不来
                    raw = {}
            clean: dict[str, Any] = {}
            for k, v in (raw.get("values") or raw).items():
                if k not in SPEC:
                    continue  # 丢弃未知键（降级/回滚后的残留）
                try:
                    clean[k] = coerce(k, v)
                except ValueError:
                    continue
            self._data = clean

    def save(self) -> None:
        with self._lock:
            payload = {
                "version": 1,
                "updated_at": time.time(),
                # exits 的键是 int，json 会转成 str，读回时 coerce 再转回来
                "values": {k: ({str(p): l for p, l in v.items()}
                               if SPEC[k]["type"] == "exits" else v)
                           for k, v in self._data.items()},
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)
        # 含 token，收紧权限
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ---------------- 读 ----------------
    def get(self, key: str) -> Any:
        """运行时配置 > 环境变量 > 代码默认值。"""
        if key not in SPEC:
            raise KeyError(key)
        with self._lock:
            if key in self._data:
                return self._data[key]
        env_v = _from_env(key)
        if env_v is not None:
            return env_v
        return SPEC[key]["default"]

    def source_of(self, key: str) -> str:
        """这个值当前是从哪来的 —— 前端据此提示「已被面板覆盖」。"""
        with self._lock:
            if key in self._data:
                return "runtime"
        return "env" if _from_env(key) is not None else "default"

    def all_values(self) -> dict[str, Any]:
        return {k: self.get(k) for k in SPEC}

    def public_view(self) -> dict[str, Any]:
        """给 WebUI 的视图：敏感值只说「配没配」，绝不回传明文。"""
        out: dict[str, Any] = {}
        for k in SPEC:
            v = self.get(k)
            if k in SECRET_KEYS:
                s = str(v or "")
                out[k] = {"set": bool(s), "hint": ("…" + s[-4:]) if len(s) >= 4 else ("已设置" if s else "")}
            elif SPEC[k]["type"] == "exits":
                out[k] = [{"port": p, "label": v[p]} for p in sorted(v)]
            else:
                out[k] = v
            out[f"{k}__source"] = self.source_of(k)
        return out

    # ---------------- 写 ----------------
    def spec_view(self) -> list[dict[str, Any]]:
        """把 schema 交给前端，让它能通用渲染表单。

        前端不该自己抄一份字段类型/可选值 —— 那正是契约错配的温床
        （schema 改了前端忘改，于是 int 字段被当字符串提交）。
        这里输出的 choices/min/max 直接驱动 input 的 type 与校验。
        """
        out: list[dict[str, Any]] = []
        for key, spec in SPEC.items():
            item: dict[str, Any] = {
                "key": key,
                "type": spec["type"],
                "env": spec.get("env") or "",
                "secret": bool(spec.get("secret")),
                "default": spec.get("default"),
                "source": self.source_of(key),
            }
            if spec.get("choices"):
                item["choices"] = list(spec["choices"])
            for bound in ("min", "max"):
                if bound in spec:
                    item[bound] = spec[bound]
            # exits/secret 类型的默认值不适合直接回显，前端有专门控件
            if spec["type"] == "exits":
                item["default"] = []
            elif spec.get("secret"):
                item["default"] = ""
            out.append(item)
        return out

    def set_many(self, updates: dict[str, Any]) -> dict[str, Any]:
        """批量写入。任一项非法则整批拒绝（不留半套配置）。"""
        clean: dict[str, Any] = {}
        for k, v in updates.items():
            if k not in SPEC:
                raise ValueError(f"未知配置项: {k}")
            clean[k] = coerce(k, v)
        with self._lock:
            self._data.update(clean)
        self.save()
        self._notify(clean)
        return clean

    def reset(self, keys: list[str] | None = None) -> None:
        """清掉运行时覆盖，回落到 env / 默认值。"""
        with self._lock:
            if keys is None:
                self._data.clear()
            else:
                for k in keys:
                    self._data.pop(k, None)
        self.save()
        self._notify({})

    # ---------------- 热生效 ----------------
    def on_change(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """注册变更回调（如把新出口表推给 ProxyManager）。"""
        self._listeners.append(fn)

    def _notify(self, changed: dict[str, Any]) -> None:
        for fn in list(self._listeners):
            try:
                fn(changed)
            except Exception:  # noqa: BLE001 —— 一个监听器炸了不该影响写入
                pass
