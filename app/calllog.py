"""
调用日志 + 模型可用性统计
========================
反代原先只在账号上累加计数，没有「哪个模型什么时候成功/失败」的时间线，
所以总览页没法回答「刚才 glm-5.3 是不是又抽了」。这里补一条轻量埋点：

* 每次 `/v1/chat/completions` 与 `/v1/messages` 落一行
* `health(window_h)` 按模型聚合成功率、延迟、最近错误，并切成 N 段时间桶
  给前端画可用性观察条

2026-09-11 增补（四件事，都由运行时配置驱动，不重启生效）
--------------------------------------------------------
1. **上行 / 下行 token 分开记**：`in_tokens` / `out_tokens`，`tokens` 保留为总数。
   历史行没有这两个字段，聚合里单独统计「有明细的行数」。
2. **日志开关**：`enabled_getter` 返回 False 时 `record()` 直接返回。
3. **保留策略**：`retention_getter` 给天数，`prune()` 删掉早于该天数的行；0 = 永不删除。
4. **变更版本号**：每次写入 `_version += 1`，SSE 端点靠它决定要不要推。

2026-09-11 存储换 SQLite
------------------------
原先是 append-only jsonl，每次查询（含 SSE 每次版本变化）都整文件解析一遍；
几千行还好，几万行后调用监控页明显发卡。换成 SQLite（标准库自带，零依赖）：

* 分页 / 时间分组 / 模型筛选全是带索引的 SQL，行数多也是毫秒级
* WAL 模式，读写互不阻塞
* **对外接口一行没变**：`record / rows / recent / query / stats / health / prune / reset`
  签名与返回结构照旧，main.py 与前端不用动
* **自动迁移**：构造时传的还是 `data/calls.jsonl` 路径，实际库文件是同目录
  `calls.db`；发现旧 jsonl 存在就整体导入，然后把它改名成 `calls.jsonl.imported`
  留档（不删）。坏行不会静默丢：计入 `stats()["legacy_bad"]`。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

MAX_LINES = 20000          # 超过就截断到一半
DEFAULT_WINDOW_H = 24
DEFAULT_BUCKETS = 24
TAIL_N = 12                # 「最近」看多少次调用
TAIL_FAIL_STREAK = 3       # 末尾连续失败几次就判 bad

# 时间分组：key -> 中文标签。since 由 range_since() 算，「当天」是本地零点
# 而不是「24 小时前」—— 这两个在下午三点差着十五个小时，用户要的是前者。
RANGES: dict[str, str] = {
    "24h": "24 小时",
    "today": "当天",
    "3d": "3 天",
    "7d": "一周",
    "30d": "一个月",
    "all": "全部",
}
DEFAULT_RANGE = "24h"
DEFAULT_PER_PAGE = 15
MAX_PER_PAGE = 200

_COLS = ("ts", "model", "ok", "endpoint", "stream", "ms", "ttft_ms", "tps",
         "tokens", "in_tokens", "out_tokens", "credits", "account",
         "key_id", "key_name", "code", "error")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    model      TEXT    NOT NULL,
    ok         INTEGER NOT NULL,
    endpoint   TEXT    NOT NULL DEFAULT 'chat',
    stream     INTEGER NOT NULL DEFAULT 0,
    ms         INTEGER NOT NULL DEFAULT 0,
    ttft_ms    INTEGER NOT NULL DEFAULT 0,
    tps        REAL    NOT NULL DEFAULT 0,
    tokens     INTEGER NOT NULL DEFAULT 0,
    in_tokens  INTEGER NOT NULL DEFAULT 0,
    out_tokens INTEGER NOT NULL DEFAULT 0,
    credits    REAL    NOT NULL DEFAULT 0,
    account    TEXT    NOT NULL DEFAULT '',
    key_id     TEXT    NOT NULL DEFAULT '',
    key_name   TEXT    NOT NULL DEFAULT '',
    code       TEXT,
    error      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_model_ts ON calls(model, ts);
"""


def range_since(key: str, now: float | None = None) -> float:
    """把分组 key 换成起始时间戳。0 = 不限。

    「当天」用 time.localtime 取本地零点 —— 服务进程启动时按 settings 的
    timezone 设过 TZ，所以这里的「今天」与签到判重是同一个口径。
    """
    now = time.time() if now is None else now
    k = (key or DEFAULT_RANGE).strip().lower()
    if k == "all":
        return 0.0
    if k == "today":
        lt = time.localtime(now)
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                            0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
    hours = {"24h": 24, "3d": 72, "7d": 168, "30d": 720}.get(k)
    if hours is None:
        hours = 24
    return now - hours * 3600


def _row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    d = {k: r[k] for k in _COLS}
    d["ok"] = bool(d["ok"])
    d["stream"] = bool(d["stream"])
    code = d["code"]
    # code 存成 TEXT 是为了同时容纳 200 / "11140" / "timeout"；读回时数字还原成 int，
    # 前端与 health() 的 str(code) 两种写法都不受影响
    if isinstance(code, str) and code.isdigit():
        d["code"] = int(code)
    elif code == "":
        d["code"] = None
    return d


class CallLog:
    def __init__(self, path: str | Path, max_lines: int = MAX_LINES,
                 enabled_getter: Callable[[], bool] | None = None,
                 retention_getter: Callable[[], int] | None = None):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # 兼容旧调用方式：给的是 calls.jsonl 就在旁边建 calls.db，并把旧文件导入
        if p.suffix == ".db":
            self.db_path = p
            self.legacy_path = p.with_suffix(".jsonl")
        else:
            self.db_path = p.with_suffix(".db")
            self.legacy_path = p
        self.path = self.db_path            # 老代码读 .path 拿「日志文件」，给库文件
        self.max_lines = max_lines
        self._lock = threading.RLock()
        self._writes = 0
        # 用 getter 而不是存值：面板改完立刻生效，不用重启也不用回调同步
        self._enabled_getter = enabled_getter
        self._retention_getter = retention_getter
        self._version = 0          # 每次写入 +1，SSE 靠它判断有没有新数据
        self._last_prune = 0.0
        self._legacy_imported = 0
        self._legacy_bad = 0
        self._conn: sqlite3.Connection | None = None
        self._open()
        self._ingest_legacy()

    # ---------------- 存储 ----------------
    def _open(self) -> None:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False,
                               timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        conn.executescript(_SCHEMA)
        self._conn = conn

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._open()
        assert self._conn is not None
        return self._conn

    def _ingest_legacy(self) -> None:
        """旧 jsonl 存在就整体导入，然后改名留档。

        不只在构造时做：测试与手工运维都可能在进程活着的时候往旧路径塞文件，
        所以每次读写前都做一次 exists() —— 一个 stat，便宜。
        """
        lp = self.legacy_path
        try:
            if not lp.exists() or lp.stat().st_size == 0:
                if lp.exists():
                    lp.unlink(missing_ok=True)
                return
        except OSError:
            return
        with self._lock:
            rows: list[tuple] = []
            bad = 0
            try:
                for ln in lp.read_text(encoding="utf-8", errors="ignore").splitlines():
                    s = ln.strip()
                    if not s:
                        continue
                    try:
                        r = json.loads(s)
                        if not isinstance(r, dict):
                            raise ValueError("not an object")
                    except Exception:  # noqa: BLE001 —— 坏行计数，不静默丢
                        bad += 1
                        continue
                    rows.append(self._tuple_of(r))
                db = self._db()
                with db:
                    db.executemany(
                        f"INSERT INTO calls ({','.join(_COLS)}) "
                        f"VALUES ({','.join('?' * len(_COLS))})", rows)
                stamp = time.strftime("%Y%m%dT%H%M%S")
                dst = lp.with_name(lp.name + ".imported")
                if dst.exists():
                    dst = lp.with_name(f"{lp.name}.imported-{stamp}")
                lp.rename(dst)
            except Exception:  # noqa: BLE001 —— 导入失败就留着旧文件，下次再试
                return
            self._legacy_imported += len(rows)
            self._legacy_bad += bad
            if rows:
                self._version += 1

    @staticmethod
    def _tuple_of(r: dict[str, Any]) -> tuple:
        it = int(r.get("in_tokens") or 0)
        ot = int(r.get("out_tokens") or 0)
        tk = int(r.get("tokens") or 0)
        if not tk and (it or ot):
            tk = it + ot
        code = r.get("code")
        return (
            float(r.get("ts") or 0),
            str(r.get("model") or "unknown"),
            1 if r.get("ok") else 0,
            str(r.get("endpoint") or "chat"),
            1 if r.get("stream") else 0,
            int(r.get("ms") or 0),
            int(r.get("ttft_ms") or 0),
            round(float(r.get("tps") or 0), 2),
            tk, it, ot,
            round(float(r.get("credits") or 0), 6),
            str(r.get("account") or ""),
            str(r.get("key_id") or ""),
            str(r.get("key_name") or ""),
            None if code is None else str(code),
            str(r.get("error") or "")[:200],
        )

    # ---------------- 开关 / 策略 ----------------
    @property
    def enabled(self) -> bool:
        if self._enabled_getter is None:
            return True
        try:
            return bool(self._enabled_getter())
        except Exception:  # noqa: BLE001 —— 配置层出问题不该让埋点崩掉调用链
            return True

    @property
    def retention_days(self) -> int:
        if self._retention_getter is None:
            return 0
        try:
            return max(0, int(self._retention_getter() or 0))
        except Exception:  # noqa: BLE001
            return 0

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    # ---------------- 写 ----------------
    def record(self, *, model: str, ok: bool, endpoint: str = "chat",
               ms: int = 0, ttft_ms: int = 0, tps: float = 0.0,
               tokens: int = 0, in_tokens: int = 0, out_tokens: int = 0,
               credits: float = 0.0,
               account: str = "", key_id: str = "", key_name: str = "",
               code: Any = None, error: str = "", stream: bool = False) -> None:
        # 开关关掉时连库都不碰。注意仍然要 return 得干净 ——
        # 这个函数的所有调用点都在请求主链路上，不能抛。
        if not self.enabled:
            return
        row = {
            "ts": round(time.time(), 3), "model": model or "unknown", "ok": bool(ok),
            "endpoint": endpoint, "stream": bool(stream), "ms": ms, "ttft_ms": ttft_ms,
            "tps": tps, "tokens": tokens, "in_tokens": in_tokens, "out_tokens": out_tokens,
            "credits": credits, "account": account, "key_id": key_id,
            "key_name": key_name, "code": code, "error": error,
        }
        with self._lock:
            self._ingest_legacy()
            try:
                db = self._db()
                with db:
                    db.execute(
                        f"INSERT INTO calls ({','.join(_COLS)}) "
                        f"VALUES ({','.join('?' * len(_COLS))})", self._tuple_of(row))
            except Exception:  # noqa: BLE001
                return
            self._writes += 1
            self._version += 1
            if self._writes % 200 == 0:
                self._truncate_if_needed()
        # 保留策略最多每小时检查一次
        if self.retention_days and time.time() - self._last_prune > 3600:
            self.prune()

    def _truncate_if_needed(self) -> None:
        """超过 max_lines 就丢掉最老的一半（防单库无限膨胀的兜底）。"""
        try:
            db = self._db()
            n = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            if n <= self.max_lines:
                return
            keep = self.max_lines // 2
            with db:
                db.execute(
                    "DELETE FROM calls WHERE id NOT IN "
                    "(SELECT id FROM calls ORDER BY ts DESC, id DESC LIMIT ?)", (keep,))
            self._version += 1
        except Exception:  # noqa: BLE001
            pass

    def prune(self, days: int | None = None) -> dict[str, Any]:
        """删掉早于 N 天的记录。days=0 或 None 且策略为 0 → 什么都不做。

        返回 {removed, kept, days}，供接口回报和测试断言。
        """
        d = self.retention_days if days is None else max(0, int(days or 0))
        if not d:
            return {"removed": 0, "kept": -1, "days": 0, "skipped": True}
        cutoff = time.time() - d * 86400
        with self._lock:
            self._last_prune = time.time()
            self._ingest_legacy()
            try:
                db = self._db()
                with db:
                    cur = db.execute("DELETE FROM calls WHERE ts < ?", (cutoff,))
                    removed = cur.rowcount if cur.rowcount is not None else 0
                kept = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            except Exception:  # noqa: BLE001
                return {"removed": 0, "kept": 0, "days": d, "error": "prune failed"}
            if removed:
                self._version += 1
            return {"removed": removed, "kept": kept, "days": d}

    # ---------------- 读 ----------------
    def rows(self, since: float = 0.0, limit: int = 0) -> list[dict[str, Any]]:
        """按时间升序返回；limit 只取最新的 N 条（仍按升序给）。"""
        with self._lock:
            self._ingest_legacy()
            try:
                db = self._db()
                if limit:
                    cur = db.execute(
                        "SELECT * FROM (SELECT * FROM calls WHERE ts >= ? "
                        "ORDER BY ts DESC, id DESC LIMIT ?) ORDER BY ts ASC, id ASC",
                        (float(since or 0), int(limit)))
                else:
                    cur = db.execute(
                        "SELECT * FROM calls WHERE ts >= ? ORDER BY ts ASC, id ASC",
                        (float(since or 0),))
                return [_row_to_dict(r) for r in cur.fetchall()]
            except Exception:  # noqa: BLE001
                return []

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(reversed(self.rows(limit=limit)))

    def query(self, *, range_key: str = DEFAULT_RANGE, page: int = 1,
              per_page: int = DEFAULT_PER_PAGE, model: str = "",
              ok: bool | None = None) -> dict[str, Any]:
        """分页查询（新→旧）。

        分页在服务端做：前端一页只要 15 条，把两万行全推过去纯属浪费 ——
        用户明确要求「一页最多 15 条，多的翻页看」。

        返回 total / pages 让前端能画页码；page 超界时夹到最后一页而不是
        回空列表（否则删日志后停在第 9 页会看到「暂无数据」，像是坏了）。
        """
        rk = (range_key or DEFAULT_RANGE).strip().lower()
        if rk not in RANGES:
            rk = DEFAULT_RANGE
        since = range_since(rk)
        where = ["ts >= ?"]
        args: list[Any] = [since]
        if model:
            where.append("model = ?")
            args.append(model)
        if ok is not None:
            where.append("ok = ?")
            args.append(1 if ok else 0)
        w = " AND ".join(where)

        per = max(1, min(MAX_PER_PAGE, int(per_page or DEFAULT_PER_PAGE)))
        with self._lock:
            self._ingest_legacy()
            try:
                db = self._db()
                total = db.execute(f"SELECT COUNT(*) FROM calls WHERE {w}", args).fetchone()[0]
                pages = max(1, (total + per - 1) // per)
                p = max(1, int(page or 1))
                if p > pages:
                    p = pages
                cur = db.execute(
                    f"SELECT * FROM calls WHERE {w} ORDER BY ts DESC, id DESC "
                    f"LIMIT ? OFFSET ?", (*args, per, (p - 1) * per))
                page_rows = [_row_to_dict(r) for r in cur.fetchall()]
            except Exception:  # noqa: BLE001
                total, pages, p, page_rows = 0, 1, 1, []

        # 本页的上下行汇总：前端页脚直接显示，不用自己再 reduce 一遍
        sum_in = sum(int(r.get("in_tokens") or 0) for r in page_rows)
        sum_out = sum(int(r.get("out_tokens") or 0) for r in page_rows)
        return {
            "calls": page_rows,
            "total": total, "page": p, "pages": pages, "per_page": per,
            "range": rk, "range_label": RANGES[rk], "since": since,
            "model": model, "ok": ok,
            "page_in_tokens": sum_in, "page_out_tokens": sum_out,
            "enabled": self.enabled,
            "retention_days": self.retention_days,
            "version": self.version,
            "generated_at": time.time(),
        }

    def stats(self) -> dict[str, Any]:
        """库层面的概况：给「日志设置」面板显示当前占用与跨度。"""
        with self._lock:
            self._ingest_legacy()
            try:
                r = self._db().execute(
                    "SELECT COUNT(*), MIN(ts), MAX(ts) FROM calls").fetchone()
                n, oldest, newest = int(r[0] or 0), float(r[1] or 0), float(r[2] or 0)
            except Exception:  # noqa: BLE001
                n, oldest, newest = 0, 0.0, 0.0
        size = 0
        for f in (self.db_path, self.db_path.with_name(self.db_path.name + "-wal")):
            try:
                size += f.stat().st_size if f.exists() else 0
            except OSError:
                pass
        return {
            "rows": n, "bytes": size,
            "oldest_ts": oldest or None, "newest_ts": newest or None,
            "enabled": self.enabled, "retention_days": self.retention_days,
            "max_lines": self.max_lines, "version": self.version,
            "storage": "sqlite", "db_path": str(self.db_path),
            "legacy_imported": self._legacy_imported,
            "legacy_bad": self._legacy_bad,
        }

    # ---------------- 聚合 ----------------
    def health(self, window_h: int = DEFAULT_WINDOW_H,
               buckets: int = DEFAULT_BUCKETS,
               known_models: list[str] | None = None,
               since: float | None = None) -> dict[str, Any]:
        """
        按模型聚合可用性。返回：
          models: [{model, total, ok, fail, rate, p50_ms, p95_ms, last_ts,
                    last_ok_ts, last_error, state, buckets:[{ok,fail,state}]}]
          state: ok(≥95%) / degraded(60~95%) / bad(<60%) / idle(窗口内无调用)

        since 显式给值时以它为准（时间分组用），此时 window_h 只用来算桶宽。
        """
        window_h = max(1, int(window_h or DEFAULT_WINDOW_H))
        buckets = max(4, min(96, int(buckets or DEFAULT_BUCKETS)))
        now = time.time()
        if since is not None and since > 0:
            span = max(60.0, now - since)
        else:
            span = window_h * 3600
            since = now - span
        if since is None or since <= 0:
            # 「全部」：跨度取最早一条到现在，没有数据就退回窗口
            first = 0.0
            with self._lock:
                try:
                    first = float(self._db().execute(
                        "SELECT MIN(ts) FROM calls").fetchone()[0] or 0)
                except Exception:  # noqa: BLE001
                    first = 0.0
            since = first or (now - window_h * 3600)
            span = max(60.0, now - since)
        bw = span / buckets

        rows = self.rows(since=since)
        agg: dict[str, dict[str, Any]] = {}

        def slot(model: str) -> dict[str, Any]:
            return agg.setdefault(model, {
                "model": model, "total": 0, "ok": 0, "fail": 0,
                "tokens": 0, "in_tokens": 0, "out_tokens": 0,
                "tok_detail_rows": 0,   # 有上下行明细的行数（老数据没有）
                "credits": 0.0, "lat": [],
                "recent": [],          # [(ts, ttft_ms, tps)] 只收成功的，用于「近期」均值
                "seq": [],             # [(ts, ok)] 全量时序，用于判「现在是不是正在挂」
                "accs": set(),         # 窗口内实际服务过该模型的账号
                "last_ts": 0.0, "last_ok_ts": 0.0, "last_error": "",
                "last_error_ts": 0.0, "codes": {},
                "buckets": [{"ok": 0, "fail": 0} for _ in range(buckets)],
            })

        for m in (known_models or []):
            slot(m)

        for r in rows:
            e = slot(r.get("model") or "unknown")
            ok = bool(r.get("ok"))
            ts = float(r.get("ts") or 0)
            e["total"] += 1
            e["ok" if ok else "fail"] += 1
            e["tokens"] += int(r.get("tokens") or 0)
            it = int(r.get("in_tokens") or 0)
            ot = int(r.get("out_tokens") or 0)
            e["in_tokens"] += it
            e["out_tokens"] += ot
            if it or ot:
                e["tok_detail_rows"] += 1
            e["credits"] = round(e["credits"] + float(r.get("credits") or 0), 6)
            if r.get("ms"):
                e["lat"].append(int(r["ms"]))
            if ok:
                e["recent"].append((ts, int(r.get("ttft_ms") or 0), float(r.get("tps") or 0)))
            e["seq"].append((ts, ok))
            if r.get("account"):
                e["accs"].add(r["account"])
            e["last_ts"] = max(e["last_ts"], ts)
            if ok:
                e["last_ok_ts"] = max(e["last_ok_ts"], ts)
            elif ts >= e["last_error_ts"]:
                e["last_error_ts"] = ts
                e["last_error"] = r.get("error") or str(r.get("code") or "")
                c = str(r.get("code") or "err")
                e["codes"][c] = e["codes"].get(c, 0) + 1
            bi = int((ts - since) / bw) if bw else 0
            bi = max(0, min(buckets - 1, bi))
            e["buckets"][bi]["ok" if ok else "fail"] += 1

        def pct(vals: list[int], p: float) -> int | None:
            if not vals:
                return None
            s = sorted(vals)
            i = min(len(s) - 1, max(0, int(round((len(s) - 1) * p))))
            return s[i]

        def state_of(ok: int, fail: int) -> str:
            t = ok + fail
            if not t:
                return "idle"
            r = ok / t
            if r >= 0.95:
                return "ok"
            if r >= 0.6:
                return "degraded"
            return "bad"

        models: list[dict[str, Any]] = []
        for e in agg.values():
            lat = e.pop("lat")
            e["p50_ms"] = pct(lat, 0.5)
            e["p95_ms"] = pct(lat, 0.95)
            # 「近期」= 最近 20 次成功调用的首字延迟 / 输出速度均值
            rec = sorted(e.pop("recent"), key=lambda x: x[0])[-20:]
            ttfts = [t for _, t, _ in rec if t > 0]
            tpss = [s for _, _, s in rec if s > 0]
            e["recent_n"] = len(rec)
            e["ttft_ms"] = int(sum(ttfts) / len(ttfts)) if ttfts else None
            e["tps"] = round(sum(tpss) / len(tpss), 1) if tpss else None
            e["accounts"] = len(e.pop("accs"))
            e["rate"] = round(e["ok"] / e["total"], 4) if e["total"] else None
            e["state"] = state_of(e["ok"], e["fail"])
            for b in e["buckets"]:
                b["state"] = state_of(b["ok"], b["fail"])
            # 尾部趋势：只看最近 TAIL_N 次调用，按次数而不是按时间桶算。
            # 桶宽在 24h/50 格下是半小时，一个模型半小时内几十次成功会把刚刚的
            # 连续失败平均掉，累计 96% 的模型「现在全挂」就还显示正常（正是要修的毛病）。
            seq = [ok for _, ok in sorted(e.pop("seq"), key=lambda x: x[0])]
            tail = seq[-TAIL_N:]
            streak = 0
            for ok in reversed(seq):
                if ok:
                    break
                streak += 1
            if not tail:
                e["tail_state"] = "idle"
            elif streak >= TAIL_FAIL_STREAK:
                e["tail_state"] = "bad"
            else:
                e["tail_state"] = state_of(sum(1 for x in tail if x),
                                           sum(1 for x in tail if not x))
            e["tail_fail_streak"] = streak
            models.append(e)

        order = {"bad": 0, "degraded": 1, "ok": 2, "idle": 3}
        # 正在挂的排最前：state 与 tail_state 取更差的那个当排序依据
        models.sort(key=lambda x: (min(order.get(x["state"], 9), order.get(x["tail_state"], 9)),
                                   -(x["total"] or 0), x["model"]))

        tot = sum(m["total"] for m in models)
        okc = sum(m["ok"] for m in models)
        # KPI：异常 = bad/degraded 或尾部已经在连续失败的；idle 单独算，不冤枉没调过的模型
        abnormal = sum(1 for m in models
                       if m["state"] in ("bad", "degraded") or m["tail_state"] == "bad")
        normal = sum(1 for m in models if m["state"] == "ok" and m["tail_state"] != "bad")
        idle = sum(1 for m in models if m["state"] == "idle")
        rates = [m["rate"] for m in models if m["rate"] is not None]
        return {
            "window_h": window_h, "buckets": buckets, "bucket_seconds": int(bw),
            "since": since, "now": now,
            "total": tot, "ok": okc, "fail": tot - okc,
            "rate": round(okc / tot, 4) if tot else None,
            "model_count": len(models),
            "abnormal": abnormal, "normal": normal, "idle": idle,
            "avg_rate": round(sum(rates) / len(rates), 4) if rates else None,
            "in_tokens": sum(m["in_tokens"] for m in models),
            "out_tokens": sum(m["out_tokens"] for m in models),
            "enabled": self.enabled,
            "retention_days": self.retention_days,
            "version": self.version,
            "models": models,
        }

    def reset(self) -> None:
        with self._lock:
            try:
                db = self._db()
                with db:
                    db.execute("DELETE FROM calls")
                db.execute("VACUUM")
            except Exception:  # noqa: BLE001
                pass
            self._writes = 0
            self._version += 1
