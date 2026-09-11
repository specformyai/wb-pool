#!/usr/bin/env python3
"""调用日志 v2 离线回归：上下行 token / 开关 / 保留策略 / 时间分组 / 分页 / 版本号。

零网络、零依赖，用临时目录，不碰生产 data/。
直接执行：.venv/bin/python tests/test_calllog_v2.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.calllog import (DEFAULT_PER_PAGE, RANGES, CallLog,  # noqa: E402
                         range_since)

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


tmp = pathlib.Path(tempfile.mkdtemp(prefix="calllog-test-"))

# =========================================================== 1. 上下行 token
print("\n[1] 上下行 token")
cl = CallLog(tmp / "a.jsonl")
cl.record(model="m1", ok=True, in_tokens=100, out_tokens=25)
rows = cl.rows()
check("落盘 1 行", len(rows), 1)
check("in_tokens 落盘", rows[0]["in_tokens"], 100)
check("out_tokens 落盘", rows[0]["out_tokens"], 25)
# 只给上下行没给 total 时自己补总数
check("tokens 自动补 = in+out", rows[0]["tokens"], 125)

# 给了 total 就不动它（上游给的 total 可能含缓存 token，不是简单相加）
cl.record(model="m1", ok=True, tokens=999, in_tokens=100, out_tokens=25)
check("显式 tokens 不被覆盖", cl.rows()[-1]["tokens"], 999)

# 两者都缺 → 不造数
cl.record(model="m1", ok=True)
last = cl.rows()[-1]
check("缺明细时 in=0", last["in_tokens"], 0)
check("缺明细时 out=0", last["out_tokens"], 0)
check("缺明细时 tokens=0", last["tokens"], 0)

# =========================================================== 2. 开关
print("\n[2] 日志开关")
flag = {"on": True}
cl2 = CallLog(tmp / "b.jsonl", enabled_getter=lambda: flag["on"])
cl2.record(model="m", ok=True, in_tokens=1)
check("开着时写入", len(cl2.rows()), 1)

flag["on"] = False
cl2.record(model="m", ok=True, in_tokens=1)
check("关掉后不再写入", len(cl2.rows()), 1)
check("enabled 属性反映 getter", cl2.enabled, False)

# 关掉时连文件都不该被 touch（这里文件已存在，验证行数没变即可）
flag["on"] = True
cl2.record(model="m", ok=True, in_tokens=1)
check("重新打开后恢复写入", len(cl2.rows()), 2)

# getter 抛异常时不能连累埋点
def boom() -> bool:
    raise RuntimeError("settings 挂了")


cl2b = CallLog(tmp / "b2.jsonl", enabled_getter=boom)
cl2b.record(model="m", ok=True)
check("getter 抛异常时默认放行", len(cl2b.rows()), 1)

# =========================================================== 3. 保留策略
print("\n[3] 保留策略 prune")
cl3 = CallLog(tmp / "c.jsonl", retention_getter=lambda: 3)
now = time.time()
with (tmp / "c.jsonl").open("w", encoding="utf-8") as f:
    for days_ago in (10, 5, 4, 2, 1, 0):
        f.write(json.dumps({"ts": now - days_ago * 86400, "model": "m",
                            "ok": True, "tokens": 0}) + "\n")
check("造了 6 行", len(cl3.rows()), 6)
res = cl3.prune()
check("按 3 天清理删掉 3 行", res["removed"], 3)
check("剩 3 行", len(cl3.rows()), 3)
check("prune 报告 days=3", res["days"], 3)

# 0 = 永不删除（用户明确要的那一档）
cl4 = CallLog(tmp / "d.jsonl", retention_getter=lambda: 0)
with (tmp / "d.jsonl").open("w", encoding="utf-8") as f:
    for days_ago in (100, 50, 1):
        f.write(json.dumps({"ts": now - days_ago * 86400, "model": "m",
                            "ok": True}) + "\n")
res4 = cl4.prune()
check("retention=0 一行都不删", res4["removed"], 0)
truthy("retention=0 标记 skipped", res4.get("skipped") is True)
check("retention=0 数据完整", len(cl4.rows()), 3)

# 显式传 days 可覆盖配置一次
res5 = cl4.prune(days=2)
check("显式 days=2 覆盖配置", res5["removed"], 2)

# 坏行不能被静默丢掉
cl5 = CallLog(tmp / "e.jsonl", retention_getter=lambda: 1)
with (tmp / "e.jsonl").open("w", encoding="utf-8") as f:
    f.write("这不是 json\n")
    f.write(json.dumps({"ts": now - 10 * 86400, "model": "m"}) + "\n")
    f.write(json.dumps({"ts": now, "model": "m"}) + "\n")
cl5.prune()
# 存储已换 SQLite：坏行不再留在文件里，而是计入 stats()["legacy_bad"]（不静默丢）
st5 = cl5.stats()
check("坏行被计数（不静默丢数据）", st5["legacy_bad"], 1)
check("过期行仍被删", st5["rows"], 1)

# =========================================================== 4. 时间分组
print("\n[4] 时间分组 range_since")
check("六档齐全", sorted(RANGES), sorted(["24h", "today", "3d", "7d", "30d", "all"]))
t0 = time.time()
check("all 不限", range_since("all", t0), 0.0)
check("24h", round(t0 - range_since("24h", t0)), 24 * 3600)
check("3d", round(t0 - range_since("3d", t0)), 72 * 3600)
check("7d", round(t0 - range_since("7d", t0)), 168 * 3600)
check("30d", round(t0 - range_since("30d", t0)), 720 * 3600)
truthy("today 落在过去 24h 内", 0 <= t0 - range_since("today", t0) <= 86400)
lt = time.localtime(range_since("today", t0))
check("today 是本地零点(时)", lt.tm_hour, 0)
check("today 是本地零点(分)", lt.tm_min, 0)
check("未知 key 回落 24h", range_since("啥玩意", t0), range_since("24h", t0))

# =========================================================== 5. 分页
print("\n[5] 分页")
cl6 = CallLog(tmp / "f.jsonl")
for i in range(38):
    cl6.record(model=f"m{i % 3}", ok=(i % 4 != 0),
               in_tokens=i, out_tokens=i * 2)
check("默认每页 15 条", DEFAULT_PER_PAGE, 15)
p1 = cl6.query(range_key="all", page=1)
check("第 1 页 15 条", len(p1["calls"]), 15)
check("总数 38", p1["total"], 38)
check("总页数 3", p1["pages"], 3)
check("per_page 回报 15", p1["per_page"], 15)
p3 = cl6.query(range_key="all", page=3)
check("第 3 页 8 条", len(p3["calls"]), 8)

# 新的在前
truthy("排序：新→旧", p1["calls"][0]["ts"] >= p1["calls"][-1]["ts"])
# 第一页第一条应该是最后写入的那条（in_tokens=37）
check("首条是最新记录", p1["calls"][0]["in_tokens"], 37)

# 超界夹到最后一页，而不是回空列表
p99 = cl6.query(range_key="all", page=99)
check("page 超界夹到最后一页", p99["page"], 3)
check("超界仍有数据", len(p99["calls"]), 8)

# 本页上下行汇总
want_in = sum(c["in_tokens"] for c in p1["calls"])
check("page_in_tokens 与本页一致", p1["page_in_tokens"], want_in)
want_out = sum(c["out_tokens"] for c in p1["calls"])
check("page_out_tokens 与本页一致", p1["page_out_tokens"], want_out)

# 筛选
fm = cl6.query(range_key="all", page=1, per_page=100, model="m1")
truthy("按模型筛选生效", all(c["model"] == "m1" for c in fm["calls"]))
truthy("筛选后有结果", len(fm["calls"]) > 0)
ff = cl6.query(range_key="all", page=1, per_page=100, ok=False)
truthy("按失败筛选生效", all(c["ok"] is False for c in ff["calls"]))
check("失败条数 = 38//4 向上", len(ff["calls"]), sum(1 for i in range(38) if i % 4 == 0))

# per_page 上限保护
big = cl6.query(range_key="all", page=1, per_page=99999)
truthy("per_page 被夹到上限", big["per_page"] <= 200)

# =========================================================== 6. 版本号
print("\n[6] 版本号（SSE 靠它判新数据）")
cl7 = CallLog(tmp / "g.jsonl")
v0 = cl7.version
cl7.record(model="m", ok=True)
truthy("record 后版本号变大", cl7.version > v0)
v1 = cl7.version
cl7.record(model="m", ok=True)
check("每次 record +1", cl7.version, v1 + 1)

# 关掉日志时版本号不该动（没有新数据就不该触发推送）
flag2 = {"on": False}
cl8 = CallLog(tmp / "h.jsonl", enabled_getter=lambda: flag2["on"])
v2 = cl8.version
cl8.record(model="m", ok=True)
check("关掉时版本号不变", cl8.version, v2)

# reset 也要 bump（否则前端不知道日志被清空了）
cl7.reset()
truthy("reset 后版本号变化", cl7.version != v1 + 1)

# =========================================================== 7. health 兼容
print("\n[7] health 向后兼容")
cl9 = CallLog(tmp / "i.jsonl")
for i in range(10):
    cl9.record(model="mA", ok=(i != 3), ms=100 + i, in_tokens=10, out_tokens=5)
h = cl9.health(window_h=24, buckets=24)
check("health 仍返回 models", isinstance(h.get("models"), list), True)
m = h["models"][0]
check("总调用 10", m["total"], 10)
check("失败 1", m["fail"], 1)
truthy("health 带 in_tokens 汇总", "in_tokens" in m)
truthy("health 带 out_tokens 汇总", "out_tokens" in m)
check("in_tokens 汇总正确", m["in_tokens"], 100)
check("out_tokens 汇总正确", m["out_tokens"], 50)

# =========================================================== 8. stats
print("\n[8] stats")
st = cl9.stats()
check("stats.rows", st["rows"], 10)
truthy("stats.bytes > 0", st["bytes"] > 0)
truthy("stats 带 oldest/newest", st["oldest_ts"] and st["newest_ts"])
truthy("stats 带 enabled", "enabled" in st)
truthy("stats 带 retention_days", "retention_days" in st)

print(f"\n{'=' * 46}\n通过 {PASS} / 失败 {FAIL}\n{'=' * 46}")
sys.exit(1 if FAIL else 0)
