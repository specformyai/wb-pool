"""
调用日志 + 模型可用性统计
========================
反代原先只在账号上累加计数，没有「哪个模型什么时候成功/失败」的时间线，
所以总览页没法回答「刚才 glm-5.3 是不是又抽了」。这里补一条轻量埋点：

* 每次 `/v1/chat/completions` 与 `/v1/messages` 落一行 `data/calls.jsonl`
* 文件超过 `MAX_LINES` 就丢掉最老的一半（append-only + 定期截断，不引依赖）
* `health(window_h)` 按模型聚合成功率、延迟、最近错误，并切成 N 段时间桶
  给前端画可用性观察条

字段刻意保持扁平，方便直接 `jq` 或 pandas 读。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

MAX_LINES = 20000          # 超过就截断到一半
DEFAULT_WINDOW_H = 24
DEFAULT_BUCKETS = 24
TAIL_N = 12                # 「最近」看多少次调用
TAIL_FAIL_STREAK = 3       # 末尾连续失败几次就判 bad


class CallLog:
    def __init__(self, path: str | Path, max_lines: int = MAX_LINES):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_lines = max_lines
        self._lock = threading.RLock()
        self._writes = 0

    # ---------------- 写 ----------------
    def record(self, *, model: str, ok: bool, endpoint: str = "chat",
               ms: int = 0, ttft_ms: int = 0, tps: float = 0.0,
               tokens: int = 0, credits: float = 0.0,
               account: str = "", key_id: str = "", key_name: str = "",
               code: Any = None, error: str = "", stream: bool = False) -> None:
        row = {
            "ts": round(time.time(), 3),
            "model": model or "unknown",
            "ok": bool(ok),
            "endpoint": endpoint,
            "stream": bool(stream),
            "ms": int(ms or 0),
            "ttft_ms": int(ttft_ms or 0),
            "tps": round(float(tps or 0), 2),
            "tokens": int(tokens or 0),
            "credits": round(float(credits or 0), 6),
            "account": account or "",
            "key_id": key_id or "",
            "key_name": key_name or "",
            "code": code,
            "error": (error or "")[:200],
        }
        line = json.dumps(row, ensure_ascii=False)
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:  # noqa: BLE001
                return
            self._writes += 1
            if self._writes % 200 == 0:
                self._truncate_if_needed()

    def _truncate_if_needed(self) -> None:
        try:
            if not self.path.exists():
                return
            lines = self.path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if len(lines) <= self.max_lines:
                return
            keep = lines[-(self.max_lines // 2):]
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            tmp.replace(self.path)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 读 ----------------
    def rows(self, since: float = 0.0, limit: int = 0) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8", errors="ignore") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        r = json.loads(ln)
                    except Exception:  # noqa: BLE001
                        continue
                    if since and (r.get("ts") or 0) < since:
                        continue
                    out.append(r)
        except Exception:  # noqa: BLE001
            return out
        if limit and len(out) > limit:
            out = out[-limit:]
        return out

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.rows()
        return list(reversed(rows[-limit:]))

    # ---------------- 聚合 ----------------
    def health(self, window_h: int = DEFAULT_WINDOW_H,
               buckets: int = DEFAULT_BUCKETS,
               known_models: list[str] | None = None) -> dict[str, Any]:
        """
        按模型聚合可用性。返回：
          models: [{model, total, ok, fail, rate, p50_ms, p95_ms, last_ts,
                    last_ok_ts, last_error, state, buckets:[{ok,fail,state}]}]
          state: ok(≥95%) / degraded(60~95%) / bad(<60%) / idle(窗口内无调用)
        """
        window_h = max(1, int(window_h or DEFAULT_WINDOW_H))
        buckets = max(4, min(96, int(buckets or DEFAULT_BUCKETS)))
        now = time.time()
        span = window_h * 3600
        since = now - span
        bw = span / buckets

        rows = self.rows(since=since)
        agg: dict[str, dict[str, Any]] = {}

        def slot(model: str) -> dict[str, Any]:
            return agg.setdefault(model, {
                "model": model, "total": 0, "ok": 0, "fail": 0,
                "tokens": 0, "credits": 0.0, "lat": [],
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
            "models": models,
        }

    def reset(self) -> None:
        with self._lock:
            try:
                self.path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            self._writes = 0
