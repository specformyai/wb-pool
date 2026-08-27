"""
app/history.py —— 账号历史对话抓取
==================================

上游没有「会话/历史」接口。唯一能拿到对话正文的地方是计费用量流水：

    POST https://www.workbuddy.cn/billing/meter/get-user-request-usage
    body {"startTime": "...", "endTime": "...", "pageNum": n, "pageSize": m}

每行字段（实测全集，没有别的）：
    requestId / credit / model / client / requestTime / inputTrunc / input / agentPurpose

两个必须知道的上游行为，写这个模块的全部前提：

1) **它不按传入范围过滤，只认「当月」。** 实测同一账号：
       08-01~08-26 → total=909
       08-15~08-20 → total=909      （范围缩小，结果不变）
       07-01~08-26 → total=0        （跨月，静默返 0，不报错）
   所以想拿全历史必须**按自然月逐月查**，不能给一个大范围一把梭。

2) **这个接口不受账号封禁影响。** 被封号（聊天接口恒回 11140 request illegal）
   查这个接口照样 200 并返回完整历史。所以「废号」也能拉记录 —— 这正是本功能
   存在的意义。反过来说：能查到流水不代表账号可用，别拿它当探活。

`input` 只有用户侧输入，上游不返回助手回复，所以前端只能渲染成单侧气泡。
部分行 `input` 为空（纯 API 调用不带用户输入字段），这类行只作为「调用」计数。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import httpx

USAGE_URL = "https://www.workbuddy.cn/billing/meter/get-user-request-usage"
IDE_UA = "WorkBuddy/5.2.6"

PAGE_SIZE = int(os.environ.get("WB_HISTORY_PAGE_SIZE", "100"))
MAX_PAGES_PER_MONTH = int(os.environ.get("WB_HISTORY_MAX_PAGES", "120"))
# 最早回溯到哪个月。账号 registered_at 不可靠（导入的号可能没有），
# 兜底用这个下界，避免无意义地往前扫几十个月。
FLOOR_YM = os.environ.get("WB_HISTORY_FLOOR", "2026-01")
TASK_TTL = 3600.0


def _headers(token: str, uid: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-User-Id": uid or "",
        "X-Ide-Type": "workbuddy-desktop",
        "X-Ide-Name": "Windows",
        "X-Ide-Version": "5.2.6",
        "User-Agent": IDE_UA,
    }


def _month_range(ym: str) -> tuple[str, str]:
    """'2026-08' -> ('2026-08-01 00:00:00', '2026-08-31 23:59:59')"""
    y, m = (int(x) for x in ym.split("-"))
    if m == 12:
        nxt = date(y + 1, 1, 1)
    else:
        nxt = date(y, m + 1, 1)
    last = nxt.toordinal() - 1
    end = date.fromordinal(last)
    return f"{ym}-01 00:00:00", f"{end.isoformat()} 23:59:59"


def _months(since_ym: str, until_ym: str) -> list[str]:
    """闭区间列出自然月，新月在前（先拉最近的，用户更关心）。"""
    def key(ym: str) -> int:
        y, m = (int(x) for x in ym.split("-"))
        return y * 12 + (m - 1)

    a, b = key(since_ym), key(until_ym)
    if a > b:
        a = b
    out = []
    for k in range(a, b + 1):
        out.append(f"{k // 12:04d}-{k % 12 + 1:02d}")
    out.reverse()
    return out


def _floor_ym(registered_at: str) -> str:
    """账号注册月与 FLOOR_YM 取较早者作为回溯下界。"""
    reg = (registered_at or "")[:7]
    ok = len(reg) == 7 and reg[4] == "-" and reg[:4].isdigit() and reg[5:].isdigit()
    if not ok:
        return FLOOR_YM
    return min(reg, FLOOR_YM) if FLOOR_YM else reg


def digits_of(phone: str) -> str:
    d = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return d[-11:] if len(d) > 11 else d


# --------------------------------------------------------------------------- #
# 单账号抓取
# --------------------------------------------------------------------------- #
def fetch_account_history(token: str, uid: str, registered_at: str = "",
                          proxy: str | None = None, timeout: float = 45.0,
                          on_progress=None) -> dict[str, Any]:
    """
    逐月分页拉全量流水。返回 {"rows": [...], "months": {...}, "errors": [...]}

    on_progress(done_months, total_months, rows_so_far) 用于任务进度回传。
    """
    until = time.strftime("%Y-%m")
    months = _months(_floor_ym(registered_at), until)
    rows: list[dict[str, Any]] = []
    per_month: dict[str, int] = {}
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()

    with httpx.Client(proxy=proxy, timeout=timeout) as c:
        for idx, ym in enumerate(months):
            start, end = _month_range(ym)
            got = 0
            total = None
            for page in range(1, MAX_PAGES_PER_MONTH + 1):
                body = {"startTime": start, "endTime": end,
                        "pageNum": page, "pageSize": PAGE_SIZE}
                try:
                    r = c.post(USAGE_URL, json=body, headers=_headers(token, uid))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{ym} p{page}: {str(exc)[:120]}")
                    break
                if r.status_code != 200:
                    errors.append(f"{ym} p{page}: HTTP {r.status_code}")
                    break
                try:
                    j = r.json()
                except Exception:  # noqa: BLE001
                    errors.append(f"{ym} p{page}: 响应非 JSON")
                    break
                data = j.get("data") or {}
                if total is None:
                    total = int(data.get("total") or 0)
                page_rows = data.get("data") or []
                if not page_rows:
                    break
                for row in page_rows:
                    key = (str(row.get("requestId") or ""), str(row.get("requestTime") or ""))
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(row)
                    got += 1
                if total and got >= total:
                    break
                if len(page_rows) < PAGE_SIZE:
                    break
            if got:
                per_month[ym] = got
            if on_progress:
                on_progress(idx + 1, len(months), len(rows))

    rows.sort(key=lambda r: str(r.get("requestTime") or ""))
    return {"rows": rows, "months": per_month, "errors": errors}


# --------------------------------------------------------------------------- #
# 会话切分 + 汇总
# --------------------------------------------------------------------------- #
def _ts(row: dict[str, Any]) -> float:
    t = str(row.get("requestTime") or "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return time.mktime(time.strptime(t, fmt))
        except ValueError:
            continue
    return 0.0


def build_sessions(rows: list[dict[str, Any]], gap_min: int = 30) -> list[dict[str, Any]]:
    """
    按时间间隔把流水切成「会话」。上游没有 conversationId，只能用间隔推断：
    相邻两条超过 gap_min 分钟就算新会话。这是启发式，不是上游的真实会话边界。
    """
    gap = max(1, int(gap_min)) * 60
    sessions: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    prev_ts = 0.0

    for row in rows:
        ts = _ts(row)
        if cur is None or (prev_ts and ts - prev_ts > gap):
            cur = {"start": str(row.get("requestTime") or ""), "end": "",
                   "rows": [], "credits": 0.0, "models": {}, "texts": 0}
            sessions.append(cur)
        cur["rows"].append(row)
        cur["end"] = str(row.get("requestTime") or "")
        try:
            cur["credits"] += float(row.get("credit") or 0)
        except (TypeError, ValueError):
            pass
        m = str(row.get("model") or "?")
        cur["models"][m] = cur["models"].get(m, 0) + 1
        if str(row.get("input") or row.get("inputTrunc") or "").strip():
            cur["texts"] += 1
        prev_ts = ts

    for s in sessions:
        s["credits"] = round(s["credits"], 4)
        s["count"] = len(s["rows"])
        s["title"] = _session_title(s["rows"])
    sessions.reverse()          # 新会话在前
    return sessions


def _session_title(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        t = str(row.get("input") or row.get("inputTrunc") or "").strip()
        if not t:
            continue
        t = " ".join(t.split())
        if len(t) > 60:
            t = t[:60] + "…"
        return t
    return "（无正文，仅 API 调用）"


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    credits = 0.0
    models: dict[str, dict[str, float]] = {}
    texts = 0
    for row in rows:
        try:
            c = float(row.get("credit") or 0)
        except (TypeError, ValueError):
            c = 0.0
        credits += c
        m = str(row.get("model") or "?")
        slot = models.setdefault(m, {"n": 0, "credits": 0.0})
        slot["n"] += 1
        slot["credits"] += c
        if str(row.get("input") or row.get("inputTrunc") or "").strip():
            texts += 1
    top = sorted(models.items(), key=lambda kv: -kv[1]["credits"])
    return {
        "count": len(rows),
        "with_text": texts,
        "credits": round(credits, 4),
        "first": str(rows[0].get("requestTime") or "") if rows else "",
        "last": str(rows[-1].get("requestTime") or "") if rows else "",
        "models": [{"model": k, "n": int(v["n"]), "credits": round(v["credits"], 4)}
                   for k, v in top],
    }


# --------------------------------------------------------------------------- #
# 缓存 + 异步任务
# --------------------------------------------------------------------------- #
class HistoryStore:
    """把抓下来的流水落盘，避免每次开页面都重打几百个上游请求。"""

    def __init__(self, dir_path: Path):
        self.dir = Path(dir_path)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _file(self, phone: str) -> Path:
        return self.dir / f"{digits_of(phone) or 'unknown'}.json"

    def save(self, phone: str, payload: dict[str, Any]) -> None:
        f = self._file(phone)
        tmp = f.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        tmp.replace(f)

    def load(self, phone: str) -> dict[str, Any] | None:
        f = self._file(phone)
        if not f.exists():
            return None
        try:
            with open(f, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def meta(self, phone: str) -> dict[str, Any]:
        f = self._file(phone)
        if not f.exists():
            return {"cached": False}
        d = self.load(phone) or {}
        return {"cached": True, "fetched_at": d.get("fetched_at", 0),
                "count": len(d.get("rows") or []),
                "size": f.stat().st_size}

    def drop(self, phone: str) -> bool:
        f = self._file(phone)
        if f.exists():
            f.unlink()
            return True
        return False


class HistoryFetcher:
    """异步抓取任务表。WebUI 用 /api/history/status/<task_id> 轮询进度。"""

    def __init__(self, pool, store: HistoryStore, proxy_mgr=None):
        self.pool = pool
        self.store = store
        self.pm = proxy_mgr
        self._tasks: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _gc(self) -> None:
        now = time.time()
        stale = [k for k, t in self._tasks.items()
                 if t.get("done") and now - t.get("finished_at", now) > TASK_TTL]
        for k in stale:
            del self._tasks[k]

    def start(self, phone: str) -> dict[str, Any]:
        acc = self.pool.find(phone)
        if not acc:
            return {"ok": False, "error": f"账号不存在: {phone}"}
        if not acc.access_token:
            return {"ok": False, "error": "该账号没有 access_token"}

        with self._lock:
            self._gc()
            for tid, t in self._tasks.items():
                if t.get("phone") == acc.phone and not t.get("done"):
                    return {"ok": True, "task_id": tid, "already": True}
            tid = uuid.uuid4().hex[:12]
            self._tasks[tid] = {
                "id": tid, "phone": acc.phone, "masked": acc.masked(),
                "done": False, "ok": False, "error": "",
                "months_done": 0, "months_total": 0, "rows": 0,
                "started_at": time.time(), "finished_at": 0.0,
            }
        th = threading.Thread(target=self._run, args=(tid, acc), daemon=True)
        th.start()
        return {"ok": True, "task_id": tid}

    def _run(self, tid: str, acc) -> None:
        def progress(done: int, total: int, rows: int) -> None:
            with self._lock:
                t = self._tasks.get(tid)
                if t:
                    t["months_done"] = done
                    t["months_total"] = total
                    t["rows"] = rows

        proxy = None
        if self.pm is not None:
            try:
                proxy = self.pm.pick()
            except Exception:  # noqa: BLE001
                proxy = None

        try:
            res = fetch_account_history(
                acc.access_token, acc.uid, acc.registered_at,
                proxy=proxy, on_progress=progress)
            payload = {
                "phone": acc.phone, "masked": acc.masked(),
                "fetched_at": time.time(),
                "months": res["months"], "errors": res["errors"],
                "rows": res["rows"],
            }
            self.store.save(acc.phone, payload)
            with self._lock:
                t = self._tasks.get(tid)
                if t:
                    t.update(done=True, ok=True, rows=len(res["rows"]),
                             errors=res["errors"], finished_at=time.time())
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                t = self._tasks.get(tid)
                if t:
                    t.update(done=True, ok=False, error=str(exc)[:300],
                             finished_at=time.time())

    def get(self, tid: str) -> dict[str, Any] | None:
        with self._lock:
            t = self._tasks.get(tid)
            return dict(t) if t else None

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            self._gc()
            return [dict(t) for t in self._tasks.values()]
