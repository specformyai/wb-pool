"""
账号池：LRU 轮询 + token 自动刷新 + 配额/失效状态机 + 原子持久化
存储：单个 JSONL 文件（每行一个账号），热加载。
"""
from __future__ import annotations

import json
import re
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from . import upstream
from .proxies import is_proxy_error

QUOTA_KEYWORDS = ("quota", "insufficient", "余额", "积分不足", "配额", "exceeded",
                  "资源包", "arrears", "额度已用尽", "额度不足")
AUTH_KEYWORDS = ("unauthorized", "invalid_token", "token expired",
                 "invalid grant", "forbidden", "request illegal")

# 上游业务码 → 分类。**优先用码判，不要在整串里搜裸数字。**
# 2026-08-24：AUTH_KEYWORDS 里原本有裸 "401"，而上游码 14018（额度已用尽）
# 的第 2-4 位恰好是 "401"，朴素子串匹配把配额问题判成鉴权失败 → status=dead。
# dead 没有自愈路径（refresh_token grant 对所有账号恒返 401），4 个还有
# 余额的号被永久除名。同类碰撞还有 13401/10401/24011/1401/4010。
# 请求前实时校验余额的阈值（见 acquire_verified）。
# 余额刷新是定时的，两次刷新之间余额可能已被打光 —— 本地读到陈旧正数，
# 调度器照样把请求发过去，必然吃一次 14018。实测 7460：本地 57.50 / 上游 0。
# 只对「余额低 且 数据陈旧」的号补一次实时查询：余额充裕的号不查，
# 避免给每个请求都加一次上游 RTT（get_balance 实测约 1.5s）。
VERIFY_BELOW_CREDITS = float(os.environ.get("WB_VERIFY_BELOW_CREDITS", "150"))
VERIFY_STALE_SEC = float(os.environ.get("WB_VERIFY_STALE_SEC", "120"))

QUOTA_CODES = frozenset({11003, 11004, 14018})
AUTH_CODES = frozenset({11140, 401, 403})


def _err_code(err: str) -> int | None:
    """从错误串里取出上游业务码。

    错误串形态是 main.py 拼的 f"{exc.code}: {exc.msg}"，正常情况下前缀就是码；
    exc.code 解析失败时前缀是 "None"，此时退回从 JSON 体里捞 "code": N。
    """
    head = err.split(":", 1)[0].strip()
    if head.isdigit():
        return int(head)
    m = re.search(r'"code"\s*:\s*(\d+)', err)
    return int(m.group(1)) if m else None


def _has_kw(low: str, words: tuple[str, ...]) -> bool:
    """纯数字关键词要求非数字边界，避免 '401' 命中 '14018' 这类子串碰撞。"""
    for w in words:
        if w.isdigit():
            if re.search(rf"(?<!\d){re.escape(w)}(?!\d)", low):
                return True
        elif w in low:
            return True
    return False


def classify_error(err: str) -> str:
    """返回 'quota' | 'auth' | 'other'。码优先，关键词兜底。"""
    if not err:
        return "other"
    code = _err_code(err)
    if code is not None:
        if code in QUOTA_CODES:
            return "quota"
        if code in AUTH_CODES:
            return "auth"
    low = err.lower()
    if _has_kw(low, QUOTA_KEYWORDS):
        return "quota"
    if _has_kw(low, AUTH_KEYWORDS):
        return "auth"
    return "other"

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
    # 签到结果留痕（2026-08-23 修）。旧实现只有 last_checkin 日期串，
    # 一旦被任何路径写成今天，定时任务整天不再复签，于是出现
    # 「签到有时给分有时不给」。现在把「确认结果」和「标记日期」分开：
    #   last_checkin_state: granted=真到账 / already=上游确认今天已签 / ""=未确认
    #   last_checkin_credit: 当天实际到账积分
    last_checkin_state: str = ""
    last_checkin_credit: float = 0.0
    # 连续签到天数（上游 daily-checkin 返回 streak_days）。旧实现拿到就丢了。
    last_checkin_streak: int = 0
    # 每日签到奖励实际到账留痕（2026-08-26 加）。上游在 00:03~00:21 自动把
    # 签到积分发成「裂变包」，比签到 cron 早，daily_checkin 只回 credit=0，
    # 于是面板上永远是 0。这两个字段由 refresh_balances() 从包体反推。
    daily_grant_credit: float = 0.0
    daily_grant_date: str = ""
    note: str = ""

    def checkin_settled(self, today: str) -> bool:
        """今天的签到是否已经有确定结果。

        仅当日期匹配 **且** 上游给过明确回执（granted/already）才算已完成。
        日期匹配但 state 为空 = 上次只写了标记没拿到结果 → 允许复签。
        """
        return self.last_checkin == today and self.last_checkin_state in ("granted", "already")

    def usable(self) -> bool:
        if self.status in ("dead", "disabled"):
            return False
        if self.status == "exhausted" and time.time() < self.cooldown_until:
            return False
        # 余额门槛：确认查过余额且为 0 的号不进候选池。
        # 没有这道门槛时，零余额号照样被 acquire() 轮到，每次都必然返回
        # 14018 —— 白烧一次上游请求 + 用户看到一次报错，才被 release()
        # 踢进 exhausted。credits_checked_at==0 表示从没查过余额（新号），
        # 此时放行，避免因为还没刷过余额就被永久排除。
        if self.credits_checked_at > 0 and self.credits_total <= 0:
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
                acc.last_error = error[:300]
                kind = classify_error(error)
                if kind == "quota":
                    acc.status = "exhausted"
                    acc.cooldown_until = time.time() + EXHAUST_COOLDOWN
                    # 上游说额度用尽，本地余额不该还挂着正数：归零并标记
                    # 已核实，这样 usable() 的余额门槛能立刻生效。
                    acc.credits_total = 0.0
                    acc.credits_checked_at = time.time()
                elif kind == "auth":
                    acc.status = "dead"
            else:
                acc.last_error = ""
                if acc.status == "exhausted" and time.time() >= acc.cooldown_until:
                    acc.status = "active"
            self.save()

    # ---------------- maintenance ----------------
    def needs_balance_verify(self, acc: Account) -> bool:
        """这个号在发请求前该不该补查一次真实余额。

        判据是「余额低 且 数据陈旧」两条同时成立：
          - credits_checked_at == 0 的新号放行（同 usable() 的逻辑，还没查过）
          - 余额 > VERIFY_BELOW_CREDITS 的号不查，省掉每请求一次 RTT
        """
        if acc.credits_checked_at <= 0:
            return False
        if acc.credits_total > VERIFY_BELOW_CREDITS:
            return False
        return (time.time() - acc.credits_checked_at) > VERIFY_STALE_SEC

    def refresh_balance(self, acc: Account,
                        proxy: str | None = None,
                        retries: int = 2) -> dict[str, Any]:
        """刷新**单个**账号余额并落库，返回上游 bal dict。

        原先只有复数版 refresh_balances()，而 main.py 的 api_invite_bind
        一直在调这个单数名字 —— 补绑邀请码成功后必抛 AttributeError。
        """
        if not acc.access_token:
            return {"total": -1.0, "error": "no access_token"}
        bal = upstream.get_balance(acc.access_token, proxy=proxy, retries=retries)
        # 代理链路故障：拉黑该出口换一个再试，不污染账号 last_error
        if bal.get("total", -1) < 0 and is_proxy_error(bal.get("error")) \
                and self.proxy_mgr:
            if proxy:
                self.proxy_mgr.mark_bad(proxy)
            proxy = self.proxy_mgr.pick()
            bal = upstream.get_balance(acc.access_token, proxy=proxy, retries=retries)
        with self._lock:
            if bal.get("total", -1) >= 0:
                acc.credits_total = bal["total"]
                acc.credits_checked_at = time.time()
                # 今日签到奖励到账额（从包体反推，见 upstream.get_balance）
                if bal.get("daily_grant_at"):
                    acc.daily_grant_credit = float(bal.get("daily_grant") or 0)
                    acc.daily_grant_date = str(bal["daily_grant_at"])[:10]
                if acc.status == "exhausted" and bal["total"] > 1:
                    acc.status = "active"
                    acc.cooldown_until = 0.0
                # 注册时间以上游为准：腾讯侧体验版套餐的 CreateTime 才是
                # 账号真实注册时间，本地 add/import 时写的是登录时间，要被覆盖
                if bal.get("registered_at"):
                    acc.registered_at = bal["registered_at"]
            elif not is_proxy_error(bal.get("error")):
                acc.last_error = f"balance: {bal.get('error', 'unknown')}"[:300]
        return bal

    def acquire_verified(self, proxy: str | None = None,
                         mode: str | None = None,
                         max_tries: int = 4) -> Account | None:
        """取号，并对「余额低且数据陈旧」的号先实时核一次余额。

        余额刷新是定时的（默认 10 分钟），两次之间余额可能已被打光。
        本地那个陈旧正数会让 usable() 的余额门槛失效，请求照发、必吃 14018。
        这里在真发请求前补一次查询，实测为 0 就标 exhausted 换下一个。

        注意：网络请求必须在 self._lock 之外做（refresh_balance 自己按需持锁），
        否则会把整个池子的调度阻塞掉一个 RTT。
        """
        tried: set[str] = set()
        acc = None
        for _ in range(max(1, max_tries)):
            acc = self.acquire(proxy=proxy, mode=mode)
            if acc is None:
                return None
            if acc.phone in tried:
                # 已经轮回到试过的号，说明候选集就这么大，别死循环
                return acc
            tried.add(acc.phone)
            if not self.needs_balance_verify(acc):
                return acc
            bal = self.refresh_balance(acc, proxy=proxy, retries=1)
            if bal.get("total", -1) < 0:
                # 查不到（链路故障等）就按原样用，不能凭查询失败误杀好号
                return acc
            if bal["total"] > 0:
                self.save()
                return acc
            # 余额实测为 0：本地归零 + 冷却，然后取下一个
            with self._lock:
                acc.credits_total = 0.0
                acc.credits_checked_at = time.time()
                acc.status = "exhausted"
                acc.cooldown_until = time.time() + EXHAUST_COOLDOWN
            self.save()
        return acc

    def refresh_balances(self, proxy: str | None = None) -> list[dict[str, Any]]:
        out = []
        for acc in self.all():
            if not acc.access_token:
                continue
            bal = self.refresh_balance(acc, proxy=proxy, retries=2)
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
        # 只有拿到过明确回执才跳过；仅有日期标记（state 为空）说明上次没结果，要复签
        if acc.checkin_settled(today) and not force:
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
                acc.last_checkin_state = "granted" if res.get("ok") else "already"
                acc.last_checkin_credit = float(res.get("credit") or 0)
                if res.get("streak_days") is not None:
                    acc.last_checkin_streak = int(res.get("streak_days") or 0)
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
            if acc.checkin_settled(today):
                out.append({"phone": acc.phone, "masked": acc.masked(),
                            "skipped": True, "reason": "already checked in today",
                            "state": acc.last_checkin_state,
                            "credit": acc.last_checkin_credit})
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
                    acc.last_checkin_state = "granted" if res.get("ok") else "already"
                    acc.last_checkin_credit = float(res.get("credit") or 0)
                    if res.get("streak_days") is not None:
                        acc.last_checkin_streak = int(res.get("streak_days") or 0)
                    if res.get("already"):
                        acc.last_error = ""
                elif is_proxy_error(res.get("error")):
                    # 链路故障：不留任何 checkin 标记，下一轮定时任务会重试。
                    # 旧实现在这里什么都不做，但 last_checkin 可能已被别处写脏。
                    if acc.last_checkin == today and not acc.last_checkin_state:
                        acc.last_checkin = ""
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
        # 总积分只算「真能用的号」：dead / disabled / 冷却中的 exhausted 都不计入，
        # 否则面板显示的额度取不出来，纯属自欺。不可用部分单独给 credits_unusable。
        usable = [a for a in accs if a.usable()]
        cr_usable = round(sum(a.credits_total for a in usable if a.credits_total > 0), 2)
        cr_all = round(sum(a.credits_total for a in accs if a.credits_total > 0), 2)
        return {
            "total": len(accs),
            "usable": len(usable),
            "by_status": by_status,
            "credits_total": cr_usable,
            "credits_total_all": cr_all,
            "credits_unusable": round(cr_all - cr_usable, 2),
            "credits_spent": round(sum(a.credits_spent for a in accs), 4),
            "requests": sum(a.request_count for a in accs),
            "tokens": sum(a.token_count for a in accs),
        }
