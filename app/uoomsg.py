"""
uoomsg 接码模块
===============
API 基础地址：https://api.uoomsg.com/zc/data.php（GET，UTF-8）

关键实测（血泪）：
  1) getMsg 接口对本账号所有号码100%返回"[尚未收到]"，不可用。
     唯一可靠路径是 queryUsed（24h 历史记录，tab 分隔）。
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


def _call(token: str, params: dict[str, str], timeout: int = 20) -> str:
    """调用 uoomsg API，返回原始文本响应"""
    params = dict(params)
    params["token"] = token
    with httpx.Client(timeout=timeout) as c:
        r = c.get(BASE, params=params)
    return r.text.strip()


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


def get_phone(token: str, max_attempts: int = 10) -> dict[str, Any]:
    """
    取一个实体卡号码（白名单过滤）。
    返回 {"ok": True, "phone": "13xxxxxxxxx"} 或 {"ok": False, "error": "..."}
    """
    rejected: list[str] = []
    for _ in range(max_attempts):
        raw = _call(token, {
            "code": "getPhone",
            "keyWord": KEYWORD,
            "cardType": "实卡",
        })
        if raw.startswith("ERROR:"):
            return {"ok": False, "error": raw}
        if not re.fullmatch(r"\d{11}", raw):
            return {"ok": False, "error": f"号码格式异常: {raw!r}"}
        if _is_virtual(raw):
            rejected.append(raw)
            release(token, raw)  # 立即释放，让号池回收
            time.sleep(0.5)
            continue
        return {"ok": True, "phone": raw, "skipped_virtual": rejected}
    return {
        "ok": False,
        "error": f"连续 {max_attempts} 次都取到非实体号段，暂停",
        "virtual_seen": rejected,
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
    """从短信正文提取6位验证码"""
    m = CODE_RE.search(content or "")
    return m.group(1) if m else None


def get_sms(token: str, phone: str, timeout_s: int = 300,
            poll_interval: int = 5) -> dict[str, Any]:
    """
    等待腾讯验证码短信，最多 timeout_s 秒。
    返回 {"ok": True, "code": "123456", "raw": "全文"} 或 {"ok": False, "error": "..."}

    数据来源是 _SmsCache 共享快照（后台每 65s 拉一次 queryUsed），
    因此这里可以用 5s 的短间隔轮询本地缓存，既灵敏又不触发平台限速。
    """
    target = _normalize(phone)
    _CACHE.ensure_running(token)

    deadline = time.time() + timeout_s
    last_error = ""
    while time.time() < deadline:
        records, last_ok, last_error = _CACHE.snapshot()
        if last_ok:
            for rec in records:
                rp = rec["phone"]
                if not rp or not target:
                    continue
                if not (rp.endswith(target) or target.endswith(rp)):
                    continue
                if KEYWORD not in rec["content"]:
                    continue
                code = extract_code(rec["content"])
                if code:
                    return {"ok": True, "code": code, "raw": rec["content"]}
        time.sleep(poll_interval)

    err = f"等待 {timeout_s}s 未收到验证码"
    if last_error:
        err += f"（queryUsed 最近错误: {last_error}）"
    return {"ok": False, "error": err}


def release(token: str, phone: str) -> str:
    """释放号码"""
    return _call(token, {"code": "release", "phone": phone})


def block(token: str, phone: str) -> str:
    """拉黑号码（注册失败时拉黑，下次不会再取到）"""
    return _call(token, {"code": "block", "phone": phone})
