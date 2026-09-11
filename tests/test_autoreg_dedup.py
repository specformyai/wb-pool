#!/usr/bin/env python3
"""
B 回归：号码去重（并发占用 + 池内已有 + 近期失败冷却 + block 收窄）

离线：假 uoomsg / 假 pool / 假 registrar，一次网络都不发。
run:  .venv/bin/python tests/test_autoreg_dedup.py
"""
from __future__ import annotations

import pathlib
import sys
import time
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import auto_register as ar          # noqa: E402
from app import uoomsg as uum                # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def ck(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {extra[:110]}" if extra else ""))


# --------------------------------------------------------------------------- #
# 假件
# --------------------------------------------------------------------------- #
class FakeAcc:
    def __init__(self, phone: str, status: str = "active") -> None:
        self.phone = phone
        self.status = status


class FakePool:
    def __init__(self, phones: list[str]) -> None:
        self._accs = [FakeAcc(p) for p in phones]

    def all(self) -> list[FakeAcc]:
        return list(self._accs)


class FakeRegistrar:
    """只记录调用，不发网络。"""

    def __init__(self, pool: FakePool) -> None:
        self.pool = pool
        self.started: list[str] = []
        self.start_ret: dict = {"ok": True, "session_id": "s1", "proxy": "direct"}
        self.finish_ret: dict = {"ok": True, "masked": "138****0000", "credits": 100}

    def start(self, phone, proxy_override=None, origin="manual", invite_code=""):
        self.started.append(phone)
        return dict(self.start_ret)

    def finish(self, session_id, code, label="", invite_code=""):
        return dict(self.finish_ret)


def mk(pool_phones=(), *, phone_seq=None):
    """构造 AutoRegistrar + 打桩 uoomsg。返回 (areg, calls)"""
    pool = FakePool(list(pool_phones))
    reg = FakeRegistrar(pool)
    areg = ar.AutoRegistrar(reg, "tok")

    calls = {"getPhone": [], "release": [], "block": [], "exclude": []}
    seq = list(phone_seq or [])

    def fake_get_phone(token, max_attempts=10, exclude=None):
        calls["exclude"].append(set(exclude or ()))
        # 复刻真实 get_phone 的排除语义：撞了就退回重取
        exn = {uum._normalize(p) for p in (exclude or set()) if p}
        skipped = []
        while seq:
            cand = seq.pop(0)
            calls["getPhone"].append(cand)
            if uum._normalize(cand) in exn:
                # 必须复刻真实 get_phone：撞号是 release 之后再 continue。
                # 少了这一步，断言「撞号被退回」会失败 —— 那是假件的错，
                # 不是产品代码的错（2026-09-11 实际踩过一次）。
                skipped.append(cand)
                calls["release"].append(cand)
                continue
            return {"ok": True, "phone": cand, "skipped_virtual": [],
                    "skipped_duplicate": skipped, "cf_blocked": 0}
        return {"ok": False, "error": "号池空了（测试）", "duplicate_seen": skipped}

    ar.uum = types.SimpleNamespace(
        _normalize=uum._normalize,
        get_phone=fake_get_phone,
        get_sms=lambda t, p, timeout_s=180, poll_interval=5: {
            "ok": True, "code": "450944", "raw": "【腾讯科技】450944为您的登录验证码"},
        release=lambda t, p: calls["release"].append(p) or "ok",
        block=lambda t, p: calls["block"].append(p) or "ok",
    )
    return areg, reg, calls


def run_task(areg, **kw):
    task = ar.AutoRegTask("t" + str(int(time.time() * 1000) % 100000), **kw)
    with areg._lock:
        areg._tasks[task.id] = task
    areg._run(task)
    return task


print("=== 自动注册号码去重回归 ===")

# --------------------------------------------------------------------------- #
# 1. claim / unclaim 基础
# --------------------------------------------------------------------------- #
areg, _, _ = mk()
ck("claim: 首次占用成功", areg._claim("13800138000") is True)
ck("claim: 重复占用被拒", areg._claim("13800138000") is False)
ck("claim: 归一化后同号也被拒", areg._claim("+8613800138000") is False)
ck("claim: 带空格同号也被拒", areg._claim("138 0013 8000") is False)
ck("claim: 不同号可以占", areg._claim("13900139000") is True)
areg._unclaim("13800138000")
ck("unclaim: 释放后可再占", areg._claim("13800138000") is True)
areg._unclaim("13800138000")
areg._unclaim("13800138000")
ck("unclaim: 幂等（重复释放不报错）", areg._claim("13800138000") is True)
ck("unclaim: None 安全", (areg._unclaim(None) or True))

# --------------------------------------------------------------------------- #
# 2. 池内已有号进入排除集合
# --------------------------------------------------------------------------- #
areg, _, _ = mk(pool_phones=["+8613800138000", "+8613900139000"])
pool_set = areg._pool_phones()
ck("pool: 池内号被归一化收集", uum._normalize("13800138000") in pool_set,
   f"{sorted(pool_set)}")
ck("pool: 两个号都在", len(pool_set) == 2)
ex = areg._exclude_set()
ck("exclude: 含池内号", uum._normalize("13900139000") in ex)

areg._claim("13700137000")
ex = areg._exclude_set()
ck("exclude: 含已占用号", uum._normalize("13700137000") in ex)
ck("exclude: 池内+占用合并", len(ex) == 3, f"{len(ex)}")

# --------------------------------------------------------------------------- #
# 3. 取号时真的把排除集合传下去了
# --------------------------------------------------------------------------- #
areg, reg, calls = mk(pool_phones=["+8613800138000"],
                      phone_seq=["13800138000", "15171738623"])
task = run_task(areg)
ck("取号: 池内已有号被跳过", reg.started == ["15171738623"], f"started={reg.started}")
ck("取号: 排除集合非空", bool(calls["exclude"] and calls["exclude"][0]),
   f"{calls['exclude'][:1]}")
ck("取号: 撞号的被 release 退回", "13800138000" in calls["release"],
   f"release={calls['release']}")
ck("取号: 任务成功", task.status == "done", task.status)
ck("取号: 成功后 claim 已释放", not areg._claimed, f"{areg._claimed}")

# --------------------------------------------------------------------------- #
# 4. 并发：两个任务不会拿到同一个号
# --------------------------------------------------------------------------- #
areg, reg, calls = mk(phone_seq=["15171738623", "15171738623", "13900139000"])
t1 = ar.AutoRegTask("c1")
with areg._lock:
    areg._tasks[t1.id] = t1
# 手动模拟 task1 已占号但还没走完
ok1 = areg._claim("15171738623")
ex = areg._exclude_set()
ck("并发: task1 占号成功", ok1 is True)
ck("并发: 排除集合含 task1 的号", uum._normalize("15171738623") in ex)

t2 = run_task(areg)
ck("并发: task2 拿到不同号", reg.started == ["13900139000"], f"started={reg.started}")
ck("并发: 撞号被退回", "15171738623" in calls["release"], f"{calls['release']}")
ck("并发: task1 的占用仍在", uum._normalize("15171738623") in areg._claimed)

# --------------------------------------------------------------------------- #
# 5. block 收窄：普通失败只 release + 冷却，不拉黑
# --------------------------------------------------------------------------- #
areg, reg, calls = mk(phone_seq=["15171738623"])
reg.finish_ret = {"ok": False, "error": "验证码校验失败：验证码错误或已过期"}
task = run_task(areg)
ck("block: 验证码失败不拉黑", calls["block"] == [], f"block={calls['block']}")
ck("block: 改为 release", "15171738623" in calls["release"], f"{calls['release']}")
ck("block: 进入近期失败冷却",
   uum._normalize("15171738623") in areg._recent_fail)
ck("block: 冷却号进排除集合",
   uum._normalize("15171738623") in areg._exclude_set())
ck("block: 任务标记失败", task.status == "failed", task.status)

# 上游明确拒绝号码 → 才拉黑
areg, reg, calls = mk(phone_seq=["15171738623"])
reg.finish_ret = {"ok": False, "error": "该手机号不支持该服务"}
run_task(areg)
ck("block: 号码被拒时拉黑", calls["block"] == ["15171738623"],
   f"block={calls['block']}")
ck("block: 拉黑时不再 release", "15171738623" not in calls["release"])

# 发码失败同样走收窄逻辑
areg, reg, calls = mk(phone_seq=["15171738623"])
reg.start_ret = {"ok": False, "error": "发码失败 HTTP 429: too many requests"}
run_task(areg)
ck("block: 发码限流不拉黑", calls["block"] == [], f"block={calls['block']}")
ck("block: 发码失败也释放", "15171738623" in calls["release"])

# --------------------------------------------------------------------------- #
# 6. 冷却 TTL 过期后放行
# --------------------------------------------------------------------------- #
areg, _, _ = mk()
areg._mark_fail("15171738623")
ck("冷却: 刚失败在排除集合内",
   uum._normalize("15171738623") in areg._exclude_set())
with areg._lock:
    areg._recent_fail[uum._normalize("15171738623")] = \
        time.time() - ar.RECENT_FAIL_TTL - 10
ck("冷却: 超过 TTL 后放行",
   uum._normalize("15171738623") not in areg._exclude_set())

# --------------------------------------------------------------------------- #
# 7. 异常/超时路径也释放 claim（finally 单点）
# --------------------------------------------------------------------------- #
areg, reg, calls = mk(phone_seq=["15171738623"])


def boom(*a, **k):
    raise RuntimeError("模拟意外异常")


reg.finish = boom
task = run_task(areg)
ck("异常: 任务失败", task.status == "failed", task.status)
ck("异常: claim 已释放（finally）", not areg._claimed, f"{areg._claimed}")

areg, reg, calls = mk(phone_seq=["15171738623"])
task = ar.AutoRegTask("stopme")
task.stop_flag = True
with areg._lock:
    areg._tasks[task.id] = task
areg._run(task)
ck("停止: claim 已释放", not areg._claimed, f"{areg._claimed}")

# --------------------------------------------------------------------------- #
# 8. 取号失败时不留下占用
# --------------------------------------------------------------------------- #
areg, reg, calls = mk(pool_phones=["+8615171738623"], phone_seq=["15171738623"])
task = run_task(areg)
ck("取号失败: 任务失败", task.status == "failed", task.status)
ck("取号失败: 没发码", reg.started == [], f"{reg.started}")
ck("取号失败: 无残留占用", not areg._claimed, f"{areg._claimed}")

# --------------------------------------------------------------------------- #
print()
print(f"{len(PASS)} passed, {len(FAIL)} failed  (total {len(PASS) + len(FAIL)})")
if FAIL:
    print("FAILED:")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
