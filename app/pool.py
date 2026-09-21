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
                 "invalid grant", "invalid_grant", "forbidden",
                 "request illegal",
                 # 上游网关（APISIX/openresty）在账号被禁时直接返回 401 HTML
                 # 错误页，body 里没有任何业务码 —— _err_code 取不到码，
                 # 而 "unauthorized" 也不出现（页面写的是 "Authorization
                 # Required"）。旧名单漏了这个词形，于是被封号的请求被判成
                 # other：不计 auth_fail、不改状态、不设冷却，号永远留在
                 # 候选池里反复撞墙（实测 4898 在 6 分钟内撞了 17 次）。
                 "authorization required",
                 # refresh_token 被吊销时 SSO 回的原文
                 "user disabled", "user not found")

# 频率限制（上游码 6004）。**这是模型级的，不是账号级的。**
# 实测同一账号在 deepseek-v4.1-flash 上 6004 的同一时刻，hy3 与 glm-5.1
# 都能正常出内容（错误文案自己也写着「您也可以切换其他模型继续使用」）。
# 所以这类错误既不该标 dead（号是好的），也不该放任不管（不管就会被
# 反复轮到、每次白烧一次请求）—— 只禁这一个模型到重置时刻。
RATE_KEYWORDS = ("频率限制", "rate limit", "too many requests",
                 "请求过于频繁")

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
RATE_CODES = frozenset({6004})
# refresh_token 被上游吊销的码。这是「账号在上游被禁」的权威判据 ——
# 比聊天接口的 11140 更强：11140 只说明这次请求被拒，而 refresh 失败且
# 回 invalid_grant/User disabled 说明凭据本身没了，本地怎么重试都救不回。
REVOKED_CODES = frozenset({12153})
REVOKED_KEYWORDS = ("invalid_grant", "user disabled", "user not found",
                    "account disabled")

# 解析不出重置时刻时的兜底冷却。上游给的「将在 X 重置」并不总可信
# （实测有比错误发生时刻还早 3 小时的值），解析到过去时间就用这个。
RATE_COOLDOWN_SEC = float(os.environ.get("WB_RATE_COOLDOWN_SEC", "1800"))
# 单个模型最长锁多久 —— 防止上游给出个离谱的远期时间把模型永久锁死。
RATE_COOLDOWN_MAX_SEC = float(os.environ.get("WB_RATE_COOLDOWN_MAX_SEC", "86400"))


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


def parse_rate_reset(err: str, now: float | None = None) -> float:
    """从 6004 错误串里解析「将在 YYYY-MM-DD HH:MM:SS UTC+8 重置」→ epoch。

    时间是固定 UTC+8 标注的，**不能用 time.mktime**（那会按本机时区解释，
    换个时区部署就整体偏移）。用 calendar.timegm 按 UTC 解释再减 8 小时。

    上游给的时刻不总可信：实测 133****8634 在 04:54 收到的 6004 写着
    「01:35:46 重置」—— 比错误本身还早 3 小时。解析到过去时间（或压根
    解析不出）就退回 RATE_COOLDOWN_SEC，并统一钳到 RATE_COOLDOWN_MAX_SEC。
    """
    import calendar
    now = time.time() if now is None else now
    m = re.search(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\s*UTC\+8", err or "")
    ts = 0.0
    if m:
        try:
            st = time.strptime(m.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S")
            ts = float(calendar.timegm(st) - 8 * 3600)
        except ValueError:
            ts = 0.0
    # 解析失败 / 已过期的时刻 → 兜底冷却
    if ts <= now + 1:
        ts = now + RATE_COOLDOWN_SEC
    return min(ts, now + RATE_COOLDOWN_MAX_SEC)


def is_revoked_error(err: str) -> bool:
    """refresh_token 被上游吊销（账号被禁）——本地无法恢复。"""
    if not err:
        return False
    code = _err_code(err)
    if code is not None and code in REVOKED_CODES:
        return True
    return _has_kw(err.lower(), REVOKED_KEYWORDS)


def classify_error(err: str) -> str:
    """返回 'quota' | 'auth' | 'other'。码优先，关键词兜底。"""
    if not err:
        return "other"
    code = _err_code(err)
    if code is not None:
        # rate 先判：6004 的文案里没有 quota/auth 类词，但顺序写死更稳
        if code in RATE_CODES:
            return "rate"
        if code in QUOTA_CODES:
            return "quota"
        if code in AUTH_CODES:
            return "auth"
    low = err.lower()
    if _has_kw(low, RATE_KEYWORDS):
        return "rate"
    if _has_kw(low, QUOTA_KEYWORDS):
        return "quota"
    if _has_kw(low, AUTH_KEYWORDS):
        return "auth"
    return "other"

AUTH_FAIL_LIMIT = int(os.environ.get("WB_AUTH_FAIL_LIMIT", "2"))
EXHAUST_COOLDOWN = 12 * 3600      # 配额耗尽冷却 12h（上游按自然日重置，双次重试窗口）
# 定时余额刷新里单个号的硬超时（秒）。串行刷新下一个卡 30s×3 次重试就是一分半，
# 36 个号全卡就是整轮拖到下一轮都没跑完。15s 已经远超正常 RTT。
BALANCE_TIMEOUT = float(os.environ.get("WB_BALANCE_TIMEOUT", "15"))
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
    # 连续 auth 类失败次数（11140/401/403）。上游对被封账号的聊天接口恒回
    # 11140 request illegal，而余额接口照样 200 —— 所以「token 能查余额」
    # 不能证明账号可用，只有真实聊天请求的回执算数。达到 AUTH_FAIL_LIMIT
    # 就永久出候选池，避免废号靠 last_used=0 抢在队头把重试预算吃光。
    # 成功一次即归零，防止网络抖动误杀好号。
    auth_fail_count: int = 0
    # 包级到期感知（缝合自 wbswitch）。上游把签到积分发成一个个独立的包，
    # 各自到期。积分消失有两条路径：被消耗（有流水）/ 包到期作废（无流水）。
    # 这三个字段由 refresh_balance() 从包体填，用来在面板上把「即将作废的
    # 额度」单独标出来，以及给 rotation_mode="expiry" 做排序依据。
    credits_expiring: float = 0.0        # 72h 内到期且还有余额的额度
    credits_expired: float = 0.0         # 已作废但上游仍列在包里的额度
    credits_expire_at: int = 0           # 最快到期的有余额包的到期时刻（ms）
    # 余额刷新异常留痕（2026-09-11 加）。定时刷新是串行的（并发会被上游风控），
    # 一个号超时会拖住整轮；现在每个号有硬超时，超时/报错就 +1 并记时间，
    # 成功一次清零。只做标签给面板看，**不改 status、不影响调度**。
    balance_fail_count: int = 0
    balance_fail_at: float = 0.0
    # 模型级限流冷却表 {model: 冷却到期 epoch}（2026-09-20 加）。
    # 上游 6004 是按「账号 × 模型」限流的，同号换个模型立刻能用。
    # 旧实现把 6004 判成 other：不改状态也不记冷却，于是被限流的号
    # 继续留在候选池里，LRU 每轮都轮到它，每次必然再吃一次 6004。
    # 只记冷却、不动 status —— 这个号对别的模型仍然是好号。
    model_limits: dict[str, float] = field(default_factory=dict)
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
        # 连续 auth 失败到顶：上游已经明确拒绝这个号的聊天接口，再轮到它
        # 只会白烧一次请求并挤掉好号的重试机会。
        if self.auth_fail_count >= AUTH_FAIL_LIMIT:
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

    def prune_model_limits(self, now: float | None = None) -> bool:
        """清掉已到期的模型限流记录。返回是否有改动（供调用方决定落盘）。"""
        now = time.time() if now is None else now
        if not self.model_limits:
            return False
        dead = [m for m, until in self.model_limits.items() if until <= now]
        for m in dead:
            self.model_limits.pop(m, None)
        return bool(dead)

    def model_available(self, model: str = "", now: float | None = None) -> bool:
        """这个号现在能不能用来打 `model`。

        model 为空（签到 / 余额 / 探针等与模型无关的路径）时恒 True ——
        限流是模型级的，不该影响这些路径。
        """
        if not model or not self.model_limits:
            return True
        now = time.time() if now is None else now
        return float(self.model_limits.get(model) or 0) <= now

    def limited_models(self, now: float | None = None) -> dict[str, float]:
        """当前仍在冷却中的模型 → 剩余秒数（面板用）。"""
        now = time.time() if now is None else now
        return {m: round(until - now, 1)
                for m, until in (self.model_limits or {}).items()
                if until > now}

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
        # rotation_mode: "lru" = 轮询（负载均衡），"drain" = 优先耗尽当前账号，
        #                "expiry" = 到期优先（先花快作废的额度，缝合自 wbswitch）
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
                if m in ("lru", "drain", "expiry"):
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
        if m not in ("lru", "drain", "expiry"):
            return False, f"未知策略 {mode}（只支持 lru / drain / expiry）"
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
                acc.auth_fail_count = 0
            self.save()
            return True

    # ---------------- rotation ----------------
    def acquire(self, proxy: str | None = None,
                mode: str | None = None,
                model: str = "") -> Account | None:
        """
        取一个可用账号，必要时先刷新 token。

        mode="lru"   轮询：取最久未使用的账号，请求摊到全池（默认）
        mode="drain" 耗尽：优先复用最近用过的那个账号，直到它 exhausted
                     再换下一个。少数账号被打光，其余保持满额。
        """
        self.reload_if_changed()
        mode = (mode or self.rotation_mode or "lru").lower()
        with self._lock:
            # 到点自动恢复：限流表里过期的条目在这里统一清掉，
            # 不依赖任何定时任务 —— 下一次取号即恢复。
            for a in self._accounts:
                a.prune_model_limits()
            cands = [a for a in self._accounts
                     if a.usable() and a.model_available(model)]
            if not cands:
                # 冷却期已过的自动复活
                now = time.time()
                for a in self._accounts:
                    if a.status == "exhausted" and now >= a.cooldown_until:
                        a.status = "active"
                cands = [a for a in self._accounts
                         if a.usable() and a.model_available(model)]
            if not cands:
                return None
            if mode == "drain":
                # 最近用过的排最前；同时把余额少的排前面，先把零头打光。
                # last_used=0（从没用过）排最后，避免每次都拉一个新号进来。
                cands.sort(key=lambda a: (-a.last_used, a.credits_total))
            elif mode == "expiry":
                # 到期优先（缝合自 wbswitch rotate.rs 的「防止积分过期浪费」）：
                # 先用最快到期的号，把快作废的额度花掉。credits_expire_at=0
                # 表示未知到期时间，排到最后而不是最前 —— 未知不等于紧急。
                cands.sort(key=lambda a: (a.credits_expire_at or (1 << 62),
                                          a.last_used))
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
        res = upstream.refresh_token(
            acc.refresh_token, proxy=proxy,
            access_tok=acc.access_token, domain=getattr(acc, "domain", ""),
        )
        with self._lock:
            if res.get("error"):
                acc.last_error = f"refresh failed: {res['error']}"[:300]
                # refresh_token 被吊销（12153 invalid_grant / User disabled）
                # = 账号在上游被禁，本地再怎么重试都救不回来。这是比聊天
                # 11140 更硬的判据，直接标 dead 并清零本地余额快照 ——
                # 那个快照是号还活着时抓的，留着会让废号在 LRU 里抢队头
                # （实测 4898 挂着 5021.69 的假余额，占着池内余额第一名）。
                if is_revoked_error(str(res.get("error"))):
                    acc.status = "dead"
                    acc.credits_total = 0.0
                    acc.credits_checked_at = time.time()
                    acc.note = (acc.note or "") or "上游吊销 refresh_token（账号被禁）"
                # 走的是 CLI 真实链路（SSO /v2/plugin/auth/token/refresh），
                # 实测各状态账号均能 200 —— 所以这里失败是真失败，不再是
                # 早先那个"端点用错导致恒 401"的假象。
                # 但仍不标 dead：access_token 本身有效期约 60 天，
                # 判活只看对话/余额路径。
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
                tokens: int = 0, credits: float = 0.0,
                model: str = "") -> None:
        """归还账号并按错误类型落状态。

        model 是本次请求实际打的模型名 —— rate（6004）必须拿到它才能
        做模型级隔离；拿不到就只能退化成记 last_error（不禁号，宁可
        再撞一次，也不要把一个只是某模型限流的好号整个停掉）。
        """
        with self._lock:
            acc.token_count += max(0, tokens)
            acc.credits_spent = round(acc.credits_spent + max(0.0, credits), 4)
            acc.prune_model_limits()
            if error:
                acc.last_error = error[:300]
                kind = classify_error(error)
                if kind == "rate":
                    # 模型级：只锁这一个模型到重置时刻，status 不动。
                    if model:
                        acc.model_limits[model] = parse_rate_reset(error)
                elif kind == "quota":
                    acc.status = "exhausted"
                    acc.cooldown_until = time.time() + EXHAUST_COOLDOWN
                    # 上游说额度用尽，本地余额不该还挂着正数：归零并标记
                    # 已核实，这样 usable() 的余额门槛能立刻生效。
                    acc.credits_total = 0.0
                    acc.credits_checked_at = time.time()
                elif kind == "auth":
                    acc.auth_fail_count += 1
                    if acc.auth_fail_count >= AUTH_FAIL_LIMIT:
                        acc.status = "dead"
            else:
                acc.last_error = ""
                acc.auth_fail_count = 0
                # 成功即证明该模型没在限流，清掉可能残留的记录
                if model:
                    acc.model_limits.pop(model, None)
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
                        retries: int = 2,
                        timeout: float = 30.0) -> dict[str, Any]:
        """刷新**单个**账号余额并落库，返回上游 bal dict。

        原先只有复数版 refresh_balances()，而 main.py 的 api_invite_bind
        一直在调这个单数名字 —— 补绑邀请码成功后必抛 AttributeError。
        """
        if not acc.access_token:
            return {"total": -1.0, "error": "no access_token"}
        bal = upstream.get_balance(acc.access_token, proxy=proxy, retries=retries,
                                   timeout=timeout)
        # 代理链路故障：拉黑该出口换一个再试，不污染账号 last_error
        if bal.get("total", -1) < 0 and is_proxy_error(bal.get("error")) \
                and self.proxy_mgr:
            if proxy:
                self.proxy_mgr.mark_bad(proxy)
            proxy = self.proxy_mgr.pick()
            bal = upstream.get_balance(acc.access_token, proxy=proxy, retries=retries,
                                       timeout=timeout)
        with self._lock:
            if bal.get("total", -1) >= 0:
                acc.credits_total = bal["total"]
                acc.credits_checked_at = time.time()
                # 包级到期状态（见 upstream.parse_pkg_expiry）
                acc.credits_expiring = float(bal.get("expiring_soon_remaining") or 0)
                acc.credits_expired = float(bal.get("expired_remaining") or 0)
                acc.credits_expire_at = int(bal.get("soonest_expire_at") or 0)
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
                # 余额接口回 401/被禁：本地那个正数快照已经不可信了。
                # 不清零的话 usable() 的余额门槛形同虚设，LRU 还会把它
                # 排在队头（余额越高越像好号），每次都白烧一次请求。
                # 注意只对 auth 类清零 —— 链路错误已被上面的分支挡掉，
                # 其它未知错误不动余额，避免凭一次查询失败误杀好号。
                if classify_error(str(bal.get("error") or "")) == "auth":
                    acc.credits_total = 0.0
                    acc.credits_checked_at = time.time()
                    acc.auth_fail_count += 1
                    if acc.auth_fail_count >= AUTH_FAIL_LIMIT:
                        acc.status = "dead"
        return bal

    def acquire_verified(self, proxy: str | None = None,
                         mode: str | None = None,
                         max_tries: int = 4,
                         model: str = "") -> Account | None:
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
            acc = self.acquire(proxy=proxy, mode=mode, model=model)
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
        """逐个刷新余额。**刻意串行**：并发打上游余额接口会触发风控。

        每个号带硬超时（BALANCE_TIMEOUT，默认 15s，重试 1 次），超时或报错
        只在账号上记 balance_fail_count / balance_fail_at 供面板标「异常」，
        不改 status —— 余额查不到不等于号不能用。
        """
        out = []
        for acc in self.all():
            if not acc.access_token:
                continue
            if acc.status in ("dead", "disabled"):
                # 停用/封号的不刷：省一次 RTT，也少一次被风控盯上的机会
                continue
            bal = self.refresh_balance(acc, proxy=proxy, retries=2,
                                       timeout=BALANCE_TIMEOUT)
            with self._lock:
                if bal.get("total", -1) >= 0:
                    acc.balance_fail_count = 0
                    acc.balance_fail_at = 0.0
                else:
                    # 超时也算：用户要的就是「超时就打标签」。链路类错误（代理挂了）
                    # 同样计数 —— 标签只是提醒人看，不参与调度，宁可多标不漏标。
                    acc.balance_fail_count += 1
                    acc.balance_fail_at = time.time()
            out.append({"phone": acc.phone, "masked": acc.masked(),
                        "total": bal.get("total"), "packages": bal.get("packages", []),
                        "error": bal.get("error"),
                        "balance_fail_count": acc.balance_fail_count})
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

    def rate_limited_for(self, model: str) -> list[dict[str, Any]]:
        """因该模型限流而被挡在候选池外的账号（用于区分 503 的真实原因）。

        「全池无可用账号」和「这个模型全部账号都在限流中」是两件事，
        后者换个模型立刻能用 —— 报错必须说清，否则用户会去查账号池。
        """
        now = time.time()
        out = []
        for a in self.all():
            if not a.usable():
                continue
            until = float((a.model_limits or {}).get(model) or 0)
            if until > now:
                out.append({"masked": a.masked(), "until": until,
                            "in_sec": round(until - now, 1)})
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
            # 到期汇总只算可用号 —— dead/disabled 号上的额度本来就取不出来，
            # 混进「即将作废」会让这个数字失去行动意义。
            "credits_expiring": round(sum(a.credits_expiring for a in usable), 2),
            "credits_expired": round(sum(a.credits_expired for a in accs), 2),
            "credits_spent": round(sum(a.credits_spent for a in accs), 4),
            "requests": sum(a.request_count for a in accs),
            "tokens": sum(a.token_count for a in accs),
        }
