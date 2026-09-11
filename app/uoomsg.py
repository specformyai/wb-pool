"""
uoomsg 接码模块
===============
API 基础地址：https://api.uoomsg.com/zc/data.php（GET，UTF-8）

关键实测（血泪）：
  0) **本 API 在 Cloudflare 后面，且会偶发返回 CF 拦截 HTML 而不是 API 文本。**
     2026-09-11 实测 A/B（各 6 次）：不带浏览器 UA 时 1/6 被拦（7342 字节
     HTML），带浏览器 UA 时 0/6。HTML 落进业务层会造成两个恶性故障：
       · get_phone 拿 HTML 去做 11 位数字全匹配 → "号码格式异常" 秒失败
       · get_sms 的守卫（不含"[尚未收到]"/"ERROR"）放行 HTML，提码正则从
         CF 页面里刮出一个 6 位数字当验证码 → 提交给腾讯必然
         "验证码校验失败"，还会顺手 block 掉一个好号
     所以 _call() 必须同时做两件事：带浏览器 UA（降低触发率）+ HTML 哨兵
     （保证 HTML 永远不会被当数据）。UA 只是概率优化，哨兵才是根治。
  1) getMsg 可用（2026-09-11 复核）。历史记录说"100%返回[尚未收到]、
     唯一可靠路径是 queryUsed"已过期 —— 当前 get_sms 走的就是 getMsg，
     正常响应形如"尚未收到包含关键字...[尚未收到]"，收到后返回短信正文。
  2) queryUsed 平台限速 1次/分钟，超限返回 ERROR。多个注册任务
     或人工调试同时调用会互相抢配额 → 必须走本模块的共享缓存
     轮询线程（_SmsCache），绝不允许业务代码直接裸调 queryUsed。
  3) 提码正则不能用 \\b：短信正文是中文
     "【腾讯科技】798304为您的登录验证码"，'4' 与 '为' 之间
     不存在 \\b 词边界（'为' 在 Unicode 模式下是 word 字符），
     re.search(r"\\b(\\d{6})\\b", ...) 恒为 None。
     必须用 (?<!\\d)(\\d{6})(?!\\d)。
  4) cardType=实卡 并不能过滤虚拟号，仍会取到 167 等号段 → 自行判段。

号段（工信部，截至2026）：
  虚拟运营商 MVNO：162 165 167 170 171
  中国移动：134-139 147 148 150-152 157-159 172 178 182-184 187 188 195 197 198
  中国联通：130-132 145 146 155 156 166 175 176 185 186 196
  中国电信：133 149 153 173 174 177 180 181 189 191 193 199
  注：173/174 是电信实体段（早期误列入虚拟段导致好号被白白丢弃）。
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any

import httpx

BASE = "https://api.uoomsg.com/zc/data.php"
KEYWORD = "腾讯科技"

# 虚拟运营商号段（前三位）——仅这5个，勿再擅自扩充
MVNO_PREFIXES = {"162", "165", "167", "170", "171"}

# 实体三大运营商号段白名单（前三位）
REAL_CARRIER_PREFIXES = {
    # 中国移动
    "134", "135", "136", "137", "138", "139", "147", "148",
    "150", "151", "152", "157", "158", "159", "172", "178",
    "182", "183", "184", "187", "188", "195", "197", "198",
    # 中国联通
    "130", "131", "132", "145", "146", "155", "156", "166",
    "175", "176", "185", "186", "196",
    # 中国电信
    "133", "149", "153", "173", "174", "177", "180", "181",
    "189", "191", "193", "199",
}

# 提码正则：中文正文中 \b 失效，必须用数字边界断言
CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def _normalize(phone: str) -> str:
    """归一化为11位纯数字（去掉 +86 / 86 前缀与所有非数字）"""
    p = re.sub(r"\D", "", phone or "")
    if p.startswith("86") and len(p) == 13:
        p = p[2:]
    return p


def _is_virtual(phone: str) -> bool:
    """
    判断是否应拒绝的号码。
    策略：白名单优先——不在实体运营商号段内的一律拒绝
    （既拦住 MVNO，也拦住未知/新增的奇怪号段）。
    """
    p = _normalize(phone)
    if len(p) != 11:
        return True
    return p[:3] not in REAL_CARRIER_PREFIXES


class UoomsgBlocked(RuntimeError):
    """上游返回了 Cloudflare 拦截页（HTML）而不是 API 文本。

    这是**可重试的瞬时故障**，不是号码问题、不是余额问题。调用方必须
    重试或如实报错，绝不能把它当成业务失败去 block 号码。
    """


# 浏览器 UA：httpx 默认 UA（python-httpx/x.y.z）更容易触发 CF 挑战。
# 实测带上后 6/6 全部正常，不带时 1/6 返回拦截 HTML。
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_CALL_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# CF 拦截页特征。正常 API 响应是纯文本（余额 "3.80"、号码 11 位数字、
# tab 分隔记录、"ERROR:..."、"...[尚未收到]"），绝不会含 HTML 标记。
_HTML_MARKERS = ("<!doctype", "<html", "<head", "<body", "cf-browser-verification",
                 "just a moment", "attention required", "cloudflare")


def _looks_like_html(raw: str) -> bool:
    """判定响应是 HTML 页面而非 API 文本。

    只在响应明显是页面时为 True —— 阈值刻意保守：正常 API 文本极短
    （余额 4 字节、号码 11 字节），而拦截页有几 KB。
    """
    low = (raw or "").lstrip().lower()
    if not low:
        return False
    if low.startswith("<"):
        return True
    return any(m in low[:2048] for m in _HTML_MARKERS)


def _call(token: str, params: dict[str, str], timeout: int = 20) -> str:
    """调用 uoomsg API，返回原始文本响应。

    HTML 响应（CF 拦截页）一律抛 UoomsgBlocked，**不返回给业务层** ——
    这是本模块最重要的一条不变式，见模块 docstring 第 0 条。
    """
    params = dict(params)
    params["token"] = token
    with httpx.Client(timeout=timeout, headers=_CALL_HEADERS) as c:
        r = c.get(BASE, params=params)
    raw = r.text.strip()
    if _looks_like_html(raw):
        raise UoomsgBlocked(
            f"uoomsg 返回 HTML（{len(raw)} 字节，疑似 Cloudflare 拦截），"
            f"code={params.get('code', '?')} HTTP {r.status_code}")
    return raw


def _parse_used(raw: str) -> list[dict[str, str]]:
    """解析 queryUsed 的 tab 分隔响应：手机号\\t价格\\t短信内容"""
    out: list[dict[str, str]] = []
    for line in raw.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        out.append({
            "phone": _normalize(parts[0]),
            "content": parts[2].strip(),
        })
    return out


class _SmsCache:
    """
    queryUsed 共享缓存。

    平台限速 1次/分钟，因此全进程只允许一个后台线程按固定间隔拉取，
    所有等码任务从缓存读 → 任意并发数都不会触发限速。
    """

    def __init__(self, interval: int = 65) -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self._records: list[dict[str, str]] = []
        self._last_ok: float = 0.0
        self._last_error: str = ""
        self._thread: threading.Thread | None = None
        self._token: str = ""

    def ensure_running(self, token: str) -> None:
        with self._lock:
            self._token = token
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._loop, name="uoomsg-queryUsed", daemon=True
            )
            self._thread.start()

    def _loop(self) -> None:
        while True:
            token = self._token
            try:
                raw = _call(token, {"code": "queryUsed"}, timeout=30)
                if raw.startswith("ERROR:"):
                    with self._lock:
                        self._last_error = raw
                else:
                    recs = _parse_used(raw)
                    with self._lock:
                        self._records = recs
                        self._last_ok = time.time()
                        self._last_error = ""
            except Exception as e:  # 网络抖动不能杀死轮询线程
                with self._lock:
                    self._last_error = f"{type(e).__name__}: {e}"
            time.sleep(self.interval)

    def snapshot(self) -> tuple[list[dict[str, str]], float, str]:
        with self._lock:
            return list(self._records), self._last_ok, self._last_error


_CACHE = _SmsCache()


def balance(token: str) -> float | str:
    """查余额，成功返回 float，失败返回错误字符串"""
    raw = _call(token, {"code": "leftAmount"})
    try:
        return float(raw)
    except ValueError:
        return raw


def get_phone(token: str, max_attempts: int = 10,
              exclude: set[str] | None = None) -> dict[str, Any]:
    """
    取一个实体卡号码（白名单过滤 + 排除集合）。
    返回 {"ok": True, "phone": "13xxxxxxxxx"} 或 {"ok": False, "error": "..."}

    exclude: 不接受的号码集合（任意格式，内部按 _normalize 归一化比对）。
             调用方应传入「其它任务已占用的号 + 池内已有账号」。
    """
    rejected: list[str] = []
    dup: list[str] = []
    blocked = 0
    exclude_norm = {_normalize(p) for p in (exclude or set()) if p}
    last_err = ""
    for _ in range(max_attempts):
        try:
            raw = _call(token, {
                "code": "getPhone",
                "keyWord": KEYWORD,
                "cardType": "实卡",
            })
        except UoomsgBlocked as exc:
            # CF 拦截是瞬时故障，重试而不是判死。原实现把 HTML 当
            # "号码格式异常" 直接 return，于是取号"秒失败"。
            blocked += 1
            last_err = str(exc)
            time.sleep(1.5)
            continue
        if raw.startswith("ERROR:"):
            return {"ok": False, "error": raw}
        if not re.fullmatch(r"\d{11}", raw):
            return {"ok": False, "error": f"号码格式异常: {raw!r}"}
        if _is_virtual(raw):
            rejected.append(raw)
            release(token, raw)  # 立即释放，让号池回收
            time.sleep(0.5)
            continue
        if _normalize(raw) in exclude_norm:
            # 平台在并发下会把同一个号发给多个请求，也可能发来池内已有的号。
            # 两种都要退回重取，否则并发任务抢同一条短信 / 白花一次短信费。
            dup.append(raw)
            release(token, raw)
            time.sleep(0.5)
            continue
        return {"ok": True, "phone": raw, "skipped_virtual": rejected,
                "skipped_duplicate": dup, "cf_blocked": blocked}
    reasons = []
    if rejected:
        reasons.append(f"{len(rejected)} 次虚拟号")
    if dup:
        reasons.append(f"{len(dup)} 次重复号")
    if blocked:
        reasons.append(f"{blocked} 次 Cloudflare 拦截")
    detail = "、".join(reasons) or "未知原因"
    return {
        "ok": False,
        "error": f"取号 {max_attempts} 次均未获得可用号码（{detail}）"
                 + (f"；最后错误: {last_err}" if last_err else ""),
        "virtual_seen": rejected,
        "duplicate_seen": dup,
        "cf_blocked": blocked,
    }


def query_used(token: str) -> list[dict[str, str]]:
    """
    读取 24h 历史记录（走共享缓存，不会触发限速）。
    首次调用会启动后台轮询线程并等待第一次拉取完成。
    """
    _CACHE.ensure_running(token)
    for _ in range(40):  # 最多等 20s 拿到首份快照
        recs, last_ok, _ = _CACHE.snapshot()
        if last_ok:
            return recs
        time.sleep(0.5)
    return []


def extract_code(content: str) -> str | None:
    """从短信正文提取6位验证码。

    ⚠️ 这个函数只做提码，**不判断 content 是不是真短信**。任何 6 位连续
    数字都会被提出来 —— 包括 CF 拦截页里的数字。判定真伪是
    _code_from_sms 的职责，业务代码不要直接拿裸响应喂给它。
    """
    m = CODE_RE.search(content or "")
    return m.group(1) if m else None


def _code_from_sms(raw: str) -> str | None:
    """从 getMsg 响应中安全提取验证码。

    正向校验：必须含关键字（腾讯短信正文形如
    "【腾讯科技】450944为您的登录验证码…"）且不是"尚未收到"/ERROR 提示。
    _call 已经拦掉 HTML，这里是第二道 —— 只认长得像目标短信的正文。
    """
    if not raw:
        return None
    if "[尚未收到]" in raw or "尚未收到" in raw or "ERROR" in raw:
        return None
    if KEYWORD not in raw:
        # 收到了某条短信但不是腾讯的（关键字过滤本该在平台侧生效），
        # 提它的码毫无意义，只会导致"验证码校验失败"。
        return None
    return extract_code(raw)


def get_sms(token: str, phone: str, timeout_s: int = 180,
            poll_interval: int = 5) -> dict[str, Any]:
    """
    等待腾讯验证码短信（getMsg 直查 + 余额监控双保险）。
    返回 {"ok": True, "code": "123456", "raw": "全文", "method": "getMsg"|"balance"}
    或 {"ok": False, "error": "..."}

    策略：
      1. getMsg 直查这个号（不走 queryUsed 避免限速风险）
      2. 同时监控余额变化（接码平台按实际收信扣费，余额变化=收到了）
      3. 两者任一判定收到即返回
    """
    target = _normalize(phone)
    deadline = time.time() + timeout_s
    cf_blocked = 0
    last_block = ""

    # 记录初始余额。CF 拦截时按"未知"处理，不能让它中断等码。
    try:
        balance_start = balance(token)
    except UoomsgBlocked as exc:
        balance_start = str(exc)
        cf_blocked += 1
        last_block = str(exc)
    balance_float = float(balance_start) if isinstance(balance_start, (int, float)) else None

    while time.time() < deadline:
        # 方法1: getMsg 直查
        try:
            raw = _call(token, {"code": "getMsg", "phone": phone, "keyWord": KEYWORD}, timeout=10)
            code = _code_from_sms(raw)
            if code:
                return {"ok": True, "code": code, "raw": raw, "method": "getMsg"}
        except UoomsgBlocked as exc:
            cf_blocked += 1
            last_block = str(exc)
        except Exception:
            pass

        # 方法2: 余额监控（收到短信会扣费，余额减少=收到了）
        if balance_float is not None:
            try:
                balance_now = balance(token)
                if isinstance(balance_now, (int, float)):
                    if balance_now < balance_float:
                        # 余额减少，说明收到了，但 getMsg 可能还没更新，再试一次
                        time.sleep(2)
                        raw = _call(token, {"code": "getMsg", "phone": phone, "keyWord": KEYWORD}, timeout=10)
                        code = _code_from_sms(raw)
                        if code:
                            return {"ok": True, "code": code, "raw": raw, "method": "balance+getMsg"}
                        # 余额变了但取不到内容，标记异常但继续等
                        balance_float = balance_now
            except UoomsgBlocked as exc:
                cf_blocked += 1
                last_block = str(exc)
            except Exception:
                pass

        time.sleep(poll_interval)

    err = f"等待 {timeout_s}s 未收到验证码（getMsg 直查 + 余额监控均无变化）"
    if cf_blocked:
        err += f"；期间 {cf_blocked} 次 Cloudflare 拦截: {last_block[:120]}"
    return {"ok": False, "error": err, "cf_blocked": cf_blocked}


def release(token: str, phone: str) -> str:
    """释放号码"""
    return _call(token, {"code": "release", "phone": phone})


def block(token: str, phone: str) -> str:
    """拉黑号码（注册失败时拉黑，下次不会再取到）"""
    return _call(token, {"code": "block", "phone": phone})
