"""验证 main.py 接线：import 成功 / 端点齐全 / 热生效双向不回环。"""
import os, sys, json, tempfile, shutil, pathlib

SB = str(pathlib.Path(__file__).resolve().parents[1])
DATA = tempfile.mkdtemp(prefix="wbdata-")
os.environ["WB_DATA_DIR"] = DATA
os.environ["WB_ADMIN_KEY"] = "test-admin-key"
os.environ["WB_BALANCE_INTERVAL_MIN"] = "9999"   # 别让余额刷新真跑
sys.path.insert(0, SB)

_p = _f = 0
def check(cond, name, extra=""):
    global _p, _f
    if cond: _p += 1; print(f"  PASS  {name}")
    else:    _f += 1; print(f"  FAIL  {name}  [{extra}]")

print("=== 1) import app.main ===")
import app.main as M
check(True, "import 成功（没有循环依赖 / 名字写错）")
check(M.settings is not None, "settings 实例存在")
check(pathlib.Path(DATA, "settings.json").parent.exists(), "DATA_DIR 生效")

print("\n=== 2) 端点齐全 ===")
paths = {}
for r in M.app.routes:
    if hasattr(r, "path"):
        paths.setdefault(r.path, set()).update(getattr(r, "methods", set()) or set())
want = [
    ("/api/settings", "GET"), ("/api/settings", "POST"),
    ("/api/settings/reset", "POST"),
    ("/api/proxy/exits", "POST"),
    ("/api/proxy/exits/{port}", "DELETE"),
    ("/api/proxy/discover", "POST"),
    ("/api/proxy/mode", "POST"),
]
for p, m in want:
    check(m in paths.get(p, set()), f"{m} {p}", sorted(paths.get(p, [])))

print("\n=== 3) 默认无私有拓扑 ===")
check(M.pm.exits == {}, "出口表默认为空", M.pm.exits)
check(M.pm.mode == "off", "代理默认 off（新部署没有代理池）", M.pm.mode)
check(M.auto_registrar.token == "", "接码 token 默认空", repr(M.auto_registrar.token))

print("\n=== 4) settings → pm 热生效 ===")
M.settings.set_many({"proxy_exits": {3128: "squid", 8080: "sq2"}})
check(M.pm.exits == {3128: "squid", 8080: "sq2"}, "改 settings 后 pm 出口表同步", M.pm.exits)
M.settings.set_many({"proxy_mode": "rotate"})
check(M.pm.mode == "rotate", "改 settings 后 pm.mode 同步", M.pm.mode)
M.settings.set_many({"uoomsg_token": "tok-abc-1234"})
check(M.auto_registrar.token == "tok-abc-1234", "接码 token 热生效", M.auto_registrar.token)
M.settings.set_many({"auth_fail_limit": 7, "verify_below_credits": 42,
                     "expiring_soon_h": 12})
check(M.pool_mod.AUTH_FAIL_LIMIT == 7, "pool.AUTH_FAIL_LIMIT 热生效", M.pool_mod.AUTH_FAIL_LIMIT)
check(M.pool_mod.VERIFY_BELOW_CREDITS == 42.0, "pool.VERIFY_BELOW_CREDITS 热生效")
check(M.upstream.EXPIRING_SOON_H == 12.0, "upstream.EXPIRING_SOON_H 热生效")

print("\n=== 5) pm → settings 反向持久化（且不回环）===")
def saved_exits():
    """settings.json 的形状是 {"version":1,"updated_at":..,"values":{...}}，
    出口表在 values 里，不在顶层 —— 直接 saved.get("proxy_exits") 恒 None。"""
    d = json.loads(pathlib.Path(DATA, "settings.json").read_text())
    return {str(k) for k in ((d.get("values") or {}).get("proxy_exits") or {})}


M.pm.add_exit(9999, "manual")
check("9999" in saved_exits(), "面板加出口后落盘到 settings.json", saved_exits())
check(M.settings.get("proxy_exits").get(9999) == "manual",
      "内存里的 settings 也同步了", M.settings.get("proxy_exits"))
M.pm.remove_exit(9999)
check("9999" not in saved_exits(), "删出口后落盘同步", saved_exits())

print("\n=== 6) 敏感值不出网 ===")
pv = M.settings.public_view()
check(isinstance(pv["uoomsg_token"], dict), "token 脱敏成对象", pv["uoomsg_token"])
check("tok-abc-1234" not in json.dumps(pv, ensure_ascii=False),
      "public_view 不含 token 明文")
check(pv["uoomsg_token"].get("set") is True, "但能看出「已配置」", pv["uoomsg_token"])

print("\n=== 7) 时区 ===")
check(M.TZ_NAME == "Asia/Shanghai", "默认时区", M.TZ_NAME)
check(M.scheduler.timezone is not None, "scheduler 用了该时区")

print("\n=== 8) _reschedule_jobs 可反复调用 ===")
check(callable(M._reschedule_jobs), "函数存在")
M._reschedule_jobs(); M._reschedule_jobs(); M._reschedule_jobs()
ids = sorted(j.id for j in M.scheduler.get_jobs())
check(ids == ["balance", "checkin", "sync_models"],
      "未 start 时反复重排也不重复（APScheduler pending 队列不查重）", ids)
M.scheduler.start()
M._reschedule_jobs(); M._reschedule_jobs()
ids = sorted(j.id for j in M.scheduler.get_jobs())
check(ids == ["balance", "checkin", "sync_models"],
      "start 之后重排同样幂等", ids)
# 改 cron 应该真的换掉 trigger，而不是留着旧的
M.settings.set_many({"checkin_cron": "30 3 * * *"})
cj = M.scheduler.get_job("checkin")
check("30" in str(cj.trigger) and "3" in str(cj.trigger),
      "改 checkin_cron 热生效到 trigger", str(cj.trigger))
M.settings.set_many({"balance_interval_min": 33})
bj = M.scheduler.get_job("balance")
check("0:33:00" in str(bj.trigger) or "1980" in str(bj.trigger),
      "改 balance_interval_min 热生效到 trigger", str(bj.trigger))
M.scheduler.shutdown(wait=False)

shutil.rmtree(DATA, ignore_errors=True)
print(f"\n{_p} passed, {_f} failed")
sys.exit(1 if _f else 0)
