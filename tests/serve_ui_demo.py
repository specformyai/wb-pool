#!/usr/bin/env python3
"""
起一个「假上游 + 假账号 + 假调用日志」的 wb-pool，专用于在浏览器里点真实 UI。

    .venv/bin/python tests/serve_ui_demo.py [port]

默认 9191。data 目录用 tempfile，绝不碰真 accounts.jsonl。
登录 admin / admin（首登会提示改密码）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TD = tempfile.mkdtemp(prefix="wbui-")
os.environ.update({
    "WB_DATA_DIR": TD,
    "WB_API_KEY": "",
    "WB_ADMIN_KEY": "",
    "WB_ACCOUNTS_FILE": TD + "/accounts.jsonl",
    "WB_PROXY_MODE": "off",
    "WB_CHECKIN_CRON": "",
    "WB_BALANCE_INTERVAL_MIN": "9999",
})

from app import main as M          # noqa: E402
from app import upstream as U      # noqa: E402
from app.pool import Account       # noqa: E402

# 假账号：2 可用 / 1 dead / 1 disabled / 1 exhausted 冷却中
SPEC = [
    ("+8613800001111", "active", 1200.0),
    ("+8613800002222", "active", 305.5),
    ("+8613800003333", "dead", 6800.0),
    ("+8613800004444", "disabled", 400.0),
    ("+8613800005555", "exhausted", 0.0),
]
accs: list[Account] = []
for ph, st, cr in SPEC:
    a = Account(phone=ph, status=st, credits_total=cr)
    a.access_token = "tok"
    a.expires_at = int((time.time() + 86400) * 1000)   # Account.expires_at 是毫秒
    a.uid = "u-" + ph[-4:]
    a.credits_spent = 12.5
    a.request_count = 30
    a.token_count = 4200
    a.last_used = time.time() - 600
    if st == "dead":
        a.last_error = "11140: 账号已被封禁"
    if st == "exhausted":
        a.cooldown_until = time.time() + 3600
    accs.append(a)
M.pool._accounts = accs

# 造 24h 调用日志：覆盖 正常 / 不稳 / 尾部连挂 / 全挂 四种形态
now = time.time()
rows: list[dict] = []


def add(model: str, ok: bool, n: int, step: float, ms: int, err: str = "",
        offset: float = 0.0, ttft: int = 0, tps: float = 0.0) -> None:
    """offset = 距今多少秒开始往前铺，用来精确控制条纹落在哪一段。"""
    for i in range(n):
        rows.append({
            "ts": round(now - offset - step * i, 3), "model": model, "ok": ok,
            "endpoint": "chat", "stream": i % 2 == 0, "ms": ms + i * 7,
            "ttft_ms": ttft + i * 13 if ok else 0,
            "tps": round(tps + (i % 5) * 0.4, 2) if ok else 0,
            "tokens": 300 + i, "credits": 0.42, "account": accs[i % 2].masked(),
            "key_id": "demo", "key_name": "cherry",
            "code": None if ok else 11140, "error": err,
        })


# glm-5.3：几乎全绿，只在中段插两撮小失败 → 徽章「正常」
add("glm-5.3", True, 120, 700, 1500, ttft=1200, tps=63.0)
add("glm-5.3", False, 3, 400, 4000, "11140: 偶发超时", offset=9 * 3600)
# kimi-k3：累计成功率高，但最近 1 小时连续失败 → 徽章要变红并标「最近连续失败」
add("kimi-k3", True, 60, 1200, 900, ttft=2600, tps=41.0, offset=3600)
add("kimi-k3", False, 14, 240, 30000, "502: upstream timeout", ttft=0)
# claude-4.6：整段都在挂 → 徽章「异常」
add("claude-4.6", False, 22, 3000, 3000, "502: upstream timeout")
add("claude-4.6", True, 1, 60, 2000, ttft=40100, tps=40.0)
# qwen-3.5：调用很少但都成功 → 大量 idle 空格
add("qwen-3.5", True, 4, 5400, 1100, ttft=800, tps=88.0)
with open(M.CALLS_FILE, "w", encoding="utf-8") as f:
    for r in sorted(rows, key=lambda x: x["ts"]):
        f.write(json.dumps(r, ensure_ascii=False) + "\n")


def fake_stream(token, payload, proxy=None, timeout=300.0):
    m = payload.get("model") or ""
    if "claude" in m:
        raise U.UpstreamError(502, 11140, "模型暂不可用（假上游）")
    yield {"choices": [{"delta": {"role": "assistant"}}]}
    for ch in "来自假上游的回复":
        yield {"choices": [{"delta": {"content": ch}}]}
    yield {"choices": [{"delta": {}, "finish_reason": "stop"}],
           "usage": {"prompt_tokens": 9, "completion_tokens": 8, "total_tokens": 17}}


M.upstream.stream_chat = fake_stream

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9191
print(f"UI demo http://127.0.0.1:{PORT}/ ｜ admin/admin ｜ data={TD}", flush=True)

import uvicorn  # noqa: E402

uvicorn.run(M.app, host="127.0.0.1", port=PORT, log_level="warning")
