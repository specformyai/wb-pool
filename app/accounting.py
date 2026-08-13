"""
按模型记账 / 倍率统计
====================
上游没有倍率接口（所有 rate/price 路径实测 404），但 **usage 里带 `credit` 字段**
—— 那是本次请求真实扣除的积分。实测确认：deepseek-v3 一次 33 tokens 的请求
返回 `"credit": 0.01`。

于是倍率不用靠余额差猜：直接累计每个模型的 credit 与 tokens，
credits_per_1k = Σcredit / (Σtokens/1000)，样本越多越准。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                try:
                    self._data = json.loads(self.path.read_text())
                except Exception:  # noqa: BLE001
                    self._data = {}

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2))
            tmp.replace(self.path)

    def record(self, model: str, usage: dict[str, Any] | None) -> float:
        """记一次请求。返回本次消耗的 credits（上游 usage.credit）。"""
        if not usage:
            return 0.0
        credit = float(usage.get("credit") or 0.0)
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        tt = int(usage.get("total_tokens") or (pt + ct))
        think = int(usage.get("completion_thinking_tokens") or 0)
        cached = int(usage.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0)

        with self._lock:
            e = self._data.setdefault(model, {
                "requests": 0, "credits": 0.0, "prompt_tokens": 0,
                "completion_tokens": 0, "total_tokens": 0,
                "thinking_tokens": 0, "cached_tokens": 0,
                "first_seen": time.time(), "last_seen": 0.0,
            })
            e["requests"] += 1
            e["credits"] = round(e["credits"] + credit, 6)
            e["prompt_tokens"] += pt
            e["completion_tokens"] += ct
            e["total_tokens"] += tt
            e["thinking_tokens"] += think
            e["cached_tokens"] += cached
            e["last_seen"] = time.time()
            self.save()
        return credit

    def table(self) -> dict[str, Any]:
        """输出倍率表：绝对 credits/1k + 以最便宜模型为 1× 的相对倍率。"""
        with self._lock:
            rows = []
            for m, e in self._data.items():
                tt = e["total_tokens"]
                per1k = round(e["credits"] / (tt / 1000), 5) if tt else None
                rows.append({
                    "model": m, "requests": e["requests"],
                    "credits": round(e["credits"], 5),
                    "prompt_tokens": e["prompt_tokens"],
                    "completion_tokens": e["completion_tokens"],
                    "total_tokens": tt,
                    "thinking_tokens": e.get("thinking_tokens", 0),
                    "credits_per_1k": per1k,
                    "credits_per_request": round(e["credits"] / e["requests"], 5)
                                           if e["requests"] else None,
                    "last_seen": e["last_seen"],
                })
        vals = [r["credits_per_1k"] for r in rows if r["credits_per_1k"]]
        base = min(vals) if vals else None
        for r in rows:
            r["multiplier"] = (round(r["credits_per_1k"] / base, 3)
                               if base and r["credits_per_1k"] else None)
        rows.sort(key=lambda r: (r["credits_per_1k"] is None, r["credits_per_1k"] or 0))
        return {"rows": rows, "base_model": next((r["model"] for r in rows
                                                  if r["credits_per_1k"] == base), None),
                "note": "上游无倍率接口；数值来自每次请求 usage.credit 累计，样本越多越准"}

    def reset(self) -> None:
        with self._lock:
            self._data = {}
            self.save()
