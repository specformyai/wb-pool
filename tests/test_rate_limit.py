"""离线回归：模型级限流隔离 + 到点自动恢复 + 分类器双向对照。

不打上游、不碰生产 accounts.jsonl —— 用 WB_ACCOUNTS_FILE 指向临时文件，
且只 import app.pool（不 import app.main，避免拉起 scheduler）。
"""
import os, sys, json, time, tempfile, traceback

# 被测代码根目录默认取本文件所在仓库，可用 WB_APP_ROOT 指向别的副本（突变对照用）。
# 不能写死绝对路径：写死会让对照实验每轮都在跑未改动的代码，全绿被误读成「断言有效」。
APP_ROOT = os.environ.get("WB_APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)

TMP = tempfile.mkdtemp(prefix="wbpool-t1-")
ACC = os.path.join(TMP, "accounts.jsonl")

from app.pool import (Account, AccountPool, classify_error, parse_rate_reset,
                      is_revoked_error, RATE_COOLDOWN_SEC, RATE_COOLDOWN_MAX_SEC,
                      AUTH_FAIL_LIMIT)

PASS, FAIL = [], []


def check(name, got, want):
    if got == want:
        PASS.append(name)
    else:
        FAIL.append(f"{name}: got={got!r} want={want!r}")


def check_true(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append(f"{name}: false {detail}")


import calendar

# 重置时刻按「当前时间 + 2 小时」动态生成，不能写死日期：写死的日期迟早会变成过去，
# parse_rate_reset 会正确地把它钳成兜底冷却，C 段就假红了。
# 文案里的时刻固定标 UTC+8，所以用 timegm 按 UTC 算再加 8 小时拼字符串，与本机时区无关。
_RESET_TS = int(time.time()) + 2 * 3600
_RESET_STR = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(_RESET_TS + 8 * 3600))
RATE_ERR = (f"6004: 您的使用量已超出频率限制，将在 {_RESET_STR} UTC+8 "
            "重置，您也可以切换其他模型继续使用。")

# ---------------------------------------------------------------- 分类器
print("=== A. classify_error 双向对照 ===")
CASES = [
    ("rate 6004 原文",            RATE_ERR,                                        "rate"),
    ("rate 无码只有文案",          "您的使用量已超出频率限制，请稍后再试",              "rate"),
    ("rate 英文",                 "429: Too Many Requests",                        "rate"),
    ("quota 14018",               "14018: 额度已用尽，请访问以下链接购买加量包",        "quota"),
    ("auth 11140",                "11140: request illegal",                        "auth"),
    ("auth 网关 401 HTML",        '<html><head><title>401 Authorization Required</title></head>'
                                  '<body><center>openresty</center></body></html>',  "auth"),
    ("auth User disabled",        "http 401: {\"code\":12153,\"msg\":\"12153:refresh token failed:"
                                  "400 Bad Request: invalid_grant: User disabled\"}",  "auth"),
    ("other 空流",                "上游返回空流",                                    "other"),
    ("other 502 断连",            "Server disconnected without sending a response.", "other"),
    ("other 11102 模型不存在",     "11102: service info not found",                  "other"),
]
for name, err, want in CASES:
    check(f"A.{name}", classify_error(err), want)

# 关键回归：14018 里的 "401" 子串不能把 quota 判成 auth
check("A.碰撞 14018 不判 auth", classify_error("14018: 额度已用尽"), "quota")
check("A.碰撞 13401 不判 auth", classify_error("13401: 某个业务错误"), "other")

print("=== B. is_revoked_error ===")
check("B.12153 User disabled", is_revoked_error(
    'http 401: {"code":12153,"msg":"12153:refresh token failed:400 Bad Request: '
    'invalid_grant: User disabled"}'), True)
check("B.普通 401 不算吊销", is_revoked_error("http 401: unauthorized"), False)
check("B.空串", is_revoked_error(""), False)

print("=== C. parse_rate_reset 解析重置时刻 ===")
now = time.time()
got = parse_rate_reset(RATE_ERR, now=now)
# 期望值 = 文案里那个 UTC+8 时刻对应的 epoch（与本机时区无关）
want_ts = float(_RESET_TS)
check_true("C.解析出文案里的重置时刻", abs(got - want_ts) < 2,
           f"got={got} want≈{want_ts}")
# 没有时间文案 → 回退默认冷却
got2 = parse_rate_reset("6004: 频率限制", now=now)
check_true("C.无时间文案回退默认冷却",
           abs(got2 - (now + RATE_COOLDOWN_SEC)) < 2, f"got={got2}")
# 过去的时间 → 不能返回过去（否则等于没锁）
past = "6004: 将在 2020-01-01 00:00:00 UTC+8 重置"
got3 = parse_rate_reset(past, now=now)
check_true("C.过去时刻被钳到未来", got3 > now, f"got={got3} now={now}")
# 离谱的未来 → 被 MAX 钳住
far = "6004: 将在 2099-01-01 00:00:00 UTC+8 重置"
got4 = parse_rate_reset(far, now=now)
check_true("C.过远时刻被 MAX 钳住",
           got4 <= now + RATE_COOLDOWN_MAX_SEC + 1, f"got={got4}")

# ---------------------------------------------------------------- 池行为
print("=== D. 模型级隔离（release → acquire 过滤）===")


def mkpool(n=3, credits=500.0):
    rows = []
    for i in range(n):
        rows.append({
            "phone": f"1390000{i:04d}", "access_token": f"tok{i}",
            "status": "active", "credits_total": credits,
            "credits_checked_at": time.time(),   # 已查过，避开余额验证路径
            "last_used": float(i),               # LRU 顺序确定
        })
    with open(ACC, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return AccountPool(ACC)


p = mkpool(3)
check("D.初始可用数", len([a for a in p.all() if a.usable()]), 3)

a0 = p.acquire(model="deepseek-v4.1-flash")
check_true("D.首次取到号", a0 is not None)
p.release(a0, error=RATE_ERR, model="deepseek-v4.1-flash")

# 该号对限流模型不可用，对其它模型仍可用
check("D.限流后 status 不变", a0.status, "active")
check("D.限流后 usable 仍为真", a0.usable(), True)
check("D.对限流模型不可用", a0.model_available("deepseek-v4.1-flash"), False)
check("D.对 hy3 仍可用", a0.model_available("hy3"), True)
check("D.对空模型名恒可用", a0.model_available(""), True)

# 再取同一模型 → 必须换号
a1 = p.acquire(model="deepseek-v4.1-flash")
check_true("D.限流模型换到别的号", a1 is not None and a1.phone != a0.phone,
           f"a1={a1 and a1.phone} a0={a0.phone}")
# 取 hy3 → LRU 应该又回到 a0（它 last_used 最早被更新？不，取过后 last_used 变新）
# 这里只断言 a0 在 hy3 的候选集里
hy3_cands = [a.phone for a in p.all() if a.usable() and a.model_available("hy3")]
check_true("D.a0 在 hy3 候选集内", a0.phone in hy3_cands, f"cands={hy3_cands}")

print("=== E. 全部号限流 → acquire 返 None + rate_limited_for 能解释 ===")
p2 = mkpool(2)
for _ in range(2):
    acc = p2.acquire(model="deepseek-v4.1-flash")
    p2.release(acc, error=RATE_ERR, model="deepseek-v4.1-flash")
check("E.全限流后取不到号", p2.acquire(model="deepseek-v4.1-flash"), None)
check_true("E.但其它模型照样取到", p2.acquire(model="hy3") is not None)
lim = p2.rate_limited_for("deepseek-v4.1-flash")
check("E.rate_limited_for 报出 2 个", len(lim), 2)
check_true("E.带剩余秒数且为正", all(x["in_sec"] > 0 for x in lim), f"{lim}")
check("E.对未限流模型为空", p2.rate_limited_for("hy3"), [])

print("=== F. 到点自动恢复（不依赖定时任务）===")
p3 = mkpool(1)
acc = p3.acquire(model="m1")
p3.release(acc, error=RATE_ERR, model="m1")
check("F.锁定后取不到", p3.acquire(model="m1"), None)
# 把冷却时刻改成已过去，模拟时间流逝
acc.model_limits["m1"] = time.time() - 1
got = p3.acquire(model="m1")
check_true("F.到点后 acquire 立刻恢复", got is not None)
check("F.过期条目被清掉", "m1" in acc.model_limits, False)

print("=== G. 成功一次清掉该模型的限流记录 ===")
p4 = mkpool(1)
acc = p4.acquire(model="m1")
p4.release(acc, error=RATE_ERR, model="m1")
check("G.先锁上", acc.model_available("m1"), False)
p4.release(acc, tokens=10, credits=0.1, model="m1")
check("G.成功后解锁", acc.model_available("m1"), True)

print("=== H. rate 不该误伤 status / 也不该被 quota-auth 分支吃掉 ===")
p5 = mkpool(1)
acc = p5.acquire(model="m1")
p5.release(acc, error=RATE_ERR, model="m1")
check("H.rate 不写 exhausted", acc.status, "active")
check("H.rate 不累加 auth_fail", acc.auth_fail_count, 0)
check("H.rate 不清零余额", acc.credits_total > 0, True)

# 对照：quota 仍然要 exhausted
p6 = mkpool(1)
acc6 = p6.acquire(model="m1")
p6.release(acc6, error="14018: 额度已用尽", model="m1")
check("H.quota 仍写 exhausted", acc6.status, "exhausted")
check("H.quota 仍清零余额", acc6.credits_total, 0.0)

# 对照：auth 累加到上限仍然 dead
p7 = mkpool(1)
acc7 = p7.acquire(model="m1")
for _ in range(AUTH_FAIL_LIMIT):
    p7.release(acc7, error="11140: request illegal", model="m1")
check("H.auth 达上限仍 dead", acc7.status, "dead")

print("=== I. model 缺省时 rate 不误锁（退化成只记 last_error）===")
p8 = mkpool(1)
acc8 = p8.acquire(model="m1")
p8.release(acc8, error=RATE_ERR)          # 不传 model
check("I.没传 model 不产生限流条目", dict(acc8.model_limits), {})
check("I.但 last_error 已记录", acc8.last_error.startswith("6004"), True)
check("I.status 不动", acc8.status, "active")

print("=== J. 落盘/重载保真（model_limits 必须持久化）===")
p9 = mkpool(1)
acc9 = p9.acquire(model="m1")
p9.release(acc9, error=RATE_ERR, model="m1")
until = acc9.model_limits["m1"]
p9b = AccountPool(ACC)            # 重新从磁盘读
a9b = p9b.all()[0]
check_true("J.重载后限流条目还在",
           abs(float(a9b.model_limits.get("m1", 0)) - until) < 1,
           f"got={a9b.model_limits}")
check("J.重载后仍对该模型不可用", a9b.model_available("m1"), False)
check("J.重载后对其它模型可用", a9b.model_available("hy3"), True)

# ---------------------------------------------------------------- 结果
print("\n" + "=" * 70)
print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
if FAIL:
    print("\n--- 失败项 ---")
    for f in FAIL:
        print("  ✗", f)
else:
    print("全部通过")
import shutil
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
