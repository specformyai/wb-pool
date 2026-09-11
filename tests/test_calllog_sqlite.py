#!/usr/bin/env python3
"""2026-09-11 二轮改动的离线回归：SQLite 调用日志迁移 / X-WB-Tries / Anthropic 用量 / 余额刷新异常标签。

零网络，用临时目录。直接执行：.venv/bin/python tests/test_calllog_sqlite.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got={got!r} want={want!r}")


def truthy(name: str, cond) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: 条件不成立")


tmp = pathlib.Path(tempfile.mkdtemp(prefix="calllog-sqlite-"))
from app.calllog import CallLog  # noqa: E402

# =========================================================== 1. 旧 jsonl 自动迁移
print("\n[1] 旧 jsonl 自动导入 SQLite")
legacy = tmp / "calls.jsonl"
now = time.time()
with legacy.open("w", encoding="utf-8") as f:
    for i in range(50):
        f.write(json.dumps({"ts": now - i * 60, "model": f"m{i % 3}", "ok": i % 5 != 3,
                            "endpoint": "chat", "ms": 100 + i, "tokens": 10 * i,
                            "code": 502 if i % 5 == 3 else 200,
                            "error": "boom" if i % 5 == 3 else ""}) + "\n")
    f.write("这行不是 json\n")
cl = CallLog(legacy)
check("库文件建在旁边", cl.db_path.name, "calls.db")
truthy("旧文件改名留档而不是删掉", (tmp / "calls.jsonl.imported").exists())
check("旧 jsonl 不再存在", legacy.exists(), False)
check("导入 50 行", cl.stats()["rows"], 50)
check("坏行计数 1", cl.stats()["legacy_bad"], 1)
check("stats.storage", cl.stats()["storage"], "sqlite")
r0 = cl.recent(limit=1)[0]
check("最新一条 model", r0["model"], "m0")
check("code 读回是 int", r0["code"], 200)
check("ok 读回是 bool", r0["ok"], True)
bad = [r for r in cl.rows() if not r["ok"]]
check("失败行 error 保留", bad[0]["error"], "boom")

# 进程活着时又有人往旧路径塞文件 → 下次读写自动并入
with legacy.open("w", encoding="utf-8") as f:
    f.write(json.dumps({"ts": now + 1, "model": "late", "ok": True}) + "\n")
check("后补的旧文件也被并入", cl.recent(limit=1)[0]["model"], "late")
check("总数 51", cl.stats()["rows"], 51)

# =========================================================== 2. 分页 SQL 与旧行为一致
print("\n[2] 分页 / 筛选")
q = cl.query(range_key="all", page=1, per_page=15)
check("total 51", q["total"], 51)
check("pages 4", q["pages"], 4)
check("首页 15 条", len(q["calls"]), 15)
truthy("新→旧", q["calls"][0]["ts"] >= q["calls"][-1]["ts"])
q4 = cl.query(range_key="all", page=99, per_page=15)
check("超界夹到最后一页", q4["page"], 4)
check("最后一页 6 条", len(q4["calls"]), 6)
qm = cl.query(range_key="all", model="m1", per_page=200)
check("按模型筛", qm["total"], sum(1 for i in range(50) if i % 3 == 1))
qf = cl.query(range_key="all", ok=False, per_page=200)
check("按失败筛", qf["total"], 10)
q24 = cl.query(range_key="24h", per_page=200)
check("24h 分组含全部（都在 50 分钟内）", q24["total"], 51)

# =========================================================== 3. 保留 / 截断 / 重置
print("\n[3] prune / truncate / reset")
cl3 = CallLog(tmp / "c.jsonl", retention_getter=lambda: 3)
with (tmp / "c.jsonl").open("w", encoding="utf-8") as f:
    for days_ago in (10, 5, 4, 2, 1, 0):
        f.write(json.dumps({"ts": now - days_ago * 86400, "model": "m", "ok": True}) + "\n")
check("造 6 行（触发导入）", cl3.stats()["rows"], 6)
res = cl3.prune()
check("按 3 天删 3 行", res["removed"], 3)
check("kept 3", res["kept"], 3)
v = cl3.version
check("显式 days=2 覆盖配置（再删 2 天前那行）", cl3.prune(days=2)["removed"], 1)
truthy("prune 有删除时版本号变化", cl3.version != v)

cl4 = CallLog(tmp / "d.jsonl", max_lines=40)
for i in range(400):
    cl4.record(model="m", ok=True, tokens=1)
truthy("超过 max_lines 后被截断到一半左右", 20 <= cl4.stats()["rows"] <= 40)
cl4.reset()
check("reset 清空", cl4.stats()["rows"], 0)
cl4.record(model="m", ok=True)
check("reset 后还能写", cl4.stats()["rows"], 1)

# =========================================================== 4. health 聚合不变
print("\n[4] health")
h = cl.health(window_h=24, buckets=24)
m0 = next(m for m in h["models"] if m["model"] == "m0")
check("m0 总数", m0["total"], sum(1 for i in range(50) if i % 3 == 0))
truthy("有 buckets", len(m0["buckets"]) == 24)
truthy("有 rate", m0["rate"] is not None)
h_all = cl.health(window_h=24, buckets=24, since=0)
truthy("since=0 走「全部」分支不崩", h_all["total"] == 51)

# =========================================================== 5. 直接给 .db 路径
print("\n[5] 直接给 .db 路径")
cl5 = CallLog(tmp / "x.db")
cl5.record(model="m", ok=True)
check("直接 .db 也能用", cl5.stats()["rows"], 1)
check("legacy 路径推导为 x.jsonl", cl5.legacy_path.name, "x.jsonl")

# =========================================================== 6. Anthropic 用量映射
print("\n[6] Anthropic usage 映射")
os.environ["WB_DATA_DIR"] = str(tmp / "data")
os.environ.setdefault("WB_PROXY_MODE", "off")
os.environ.setdefault("WB_BALANCE_INTERVAL_MIN", "9999")
from app import main as M  # noqa: E402

check("无缓存字段：input=prompt", M._anthropic_usage({"prompt_tokens": 30, "completion_tokens": 8}),
      {"input_tokens": 30, "output_tokens": 8})
check("DeepSeek 写法 prompt_cache_hit_tokens",
      M._anthropic_usage({"prompt_tokens": 100, "completion_tokens": 5, "prompt_cache_hit_tokens": 60}),
      {"input_tokens": 40, "output_tokens": 5, "cache_read_input_tokens": 60, "cache_creation_input_tokens": 0})
check("OpenAI 写法 prompt_tokens_details.cached_tokens",
      M._anthropic_usage({"prompt_tokens": 100, "completion_tokens": 5,
                          "prompt_tokens_details": {"cached_tokens": 30}}),
      {"input_tokens": 70, "output_tokens": 5, "cache_read_input_tokens": 30, "cache_creation_input_tokens": 0})
check("缓存超过 prompt 时钳位", M._anthropic_usage({"prompt_tokens": 10, "cached_tokens": 99})["input_tokens"], 0)
check("usage=None 不崩", M._anthropic_usage(None), {"input_tokens": 0, "output_tokens": 0})

# =========================================================== 7. X-WB-Tries 头
print("\n[7] X-WB-Tries 头")
from app.pool import Account  # noqa: E402

acc = Account(phone="+8613800000001", uid="u", access_token="t")
h1 = M._tries_headers(acc, [acc.masked()])
check("一次成功：Tries=1", h1["X-WB-Tries"], "1")
check("一次成功：带 Account", h1["X-WB-Account"], acc.masked())
truthy("一次成功：无 Swapped", "X-WB-Swapped" not in h1)
h2 = M._tries_headers(acc, ["138****0009", "138****0008", acc.masked()])
check("换了两次：Tries=3", h2["X-WB-Tries"], "3")
check("换了两次：Swapped 列出被换掉的", h2["X-WB-Swapped"], "138****0009,138****0008")
h3 = M._tries_headers(None, ["a", "b"])
truthy("全失败：无 Account", "X-WB-Account" not in h3)
check("全失败：Swapped 全列", h3["X-WB-Swapped"], "a,b")

# =========================================================== 8. 余额刷新异常标签
print("\n[8] 余额刷新超时/报错打标签，不改 status")
from app import pool as P  # noqa: E402
from app import upstream as U  # noqa: E402

pl = P.AccountPool(tmp / "acc.jsonl")
good = Account(phone="+8613800000011", uid="a", access_token="ta")
badacc = Account(phone="+8613800000012", uid="b", access_token="tb")
dis = Account(phone="+8613800000013", uid="c", access_token="tc", status="disabled")
for a in (good, badacc, dis):
    pl.add(a)
calls: list[tuple[str, float]] = []


def fake_balance(token, proxy=None, retries=3, timeout=30.0):
    calls.append((token, timeout))
    if token == "tb":
        return {"total": -1.0, "packages": [], "error": "timed out"}
    return {"total": 123.0, "packages": [], "registered_at": ""}


orig = U.get_balance
U.get_balance = fake_balance
try:
    out = pl.refresh_balances()
finally:
    U.get_balance = orig
check("disabled 号被跳过（省 RTT 少风控）", sorted(t for t, _ in calls), ["ta", "tb"])
truthy("每次调用都带硬超时", all(t == P.BALANCE_TIMEOUT for _, t in calls))
g = pl.find(good.phone); b = pl.find(badacc.phone)
check("成功号 fail_count=0", g.balance_fail_count, 0)
check("超时号 fail_count=1", b.balance_fail_count, 1)
truthy("超时号记了时间", b.balance_fail_at > 0)
check("超时号 status 不变", b.status, "active")
truthy("超时属链路错误，不污染 last_error（原有约定）", not b.last_error)
check("结果里带 balance_fail_count", [o["balance_fail_count"] for o in out], [0, 1])
U.get_balance = lambda token, proxy=None, retries=3, timeout=30.0: {"total": 5.0, "packages": []}
try:
    pl.refresh_balances()
finally:
    U.get_balance = orig
check("恢复后 fail_count 清零", pl.find(badacc.phone).balance_fail_count, 0)
# 落盘再读回：新字段要能持久化，老文件没有该字段也要能读
pl2 = P.AccountPool(tmp / "acc.jsonl")
truthy("新字段可持久化读回", hasattr(pl2.find(good.phone), "balance_fail_count"))

print(f"\n{'=' * 46}\n通过 {PASS} / 失败 {FAIL}\n{'=' * 46}")
sys.exit(1 if FAIL else 0)
