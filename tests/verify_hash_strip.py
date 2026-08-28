#!/usr/bin/env python3
"""验证 hash 抹除：地址栏不再显露 #/overview，且登录后仍回到原页面。

要证四件事
----------
  1. 未登录访问 /#/overview -> 落在 /login，地址栏 hash 为空、search 为空
  2. 换个 hash（/#/pool）行为一致 —— 不是写死某个页面
  3. 登录成功后回到 /#/overview —— 落点没丢，用户仍到他原本要去的页
  4. 无 hash 的普通访问不受影响（回归）

在本地实例上跑（用真实 HTTP + 真实浏览器），不碰生产。

跑法
----
  python tests/serve_verify.py &            # 起隔离实例（临时 WB_DATA_DIR）
  python tests/verify_hash_strip.py         # 需要 headless chrome 的 CDP 端口

必须用**干净实例**：用例 4 要以默认密码 admin 登录，跑过一次后密码已被改掉。
WB_VERIFY_BASE 可覆盖被测地址，WB_CDP_URL 可覆盖 CDP 地址。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import cdp  # noqa: E402

BASE = os.environ.get("WB_VERIFY_BASE", "http://127.0.0.1:8931")

_pass = _fail = 0


def check(cond: bool, name: str, extra="") -> None:
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        print(f"  FAIL  {name}   {extra}")


def fresh_page() -> cdp.Page:
    """开一张新 tab。

    Chrome 148 的 /json/new 只接受 PUT，用 GET 会得到
    `HTTP Error 405: Method Not Allowed`（本轮踩过）。老版本只认 GET，
    所以先 PUT 再退回 GET。
    """
    url = f"{cdp.CDP_HTTP}/json/new?about:blank"
    try:
        req = urllib.request.Request(url, method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            info = json.loads(r.read().decode())
    except urllib.error.HTTPError:
        with urllib.request.urlopen(url, timeout=10) as r:
            info = json.loads(r.read().decode())
    return cdp.Page(info["webSocketDebuggerUrl"])


def snap(pg: cdp.Page) -> dict:
    """读页面状态。

    落点不能靠「事后读 sessionStorage 残留」判断
    ------------------------------------------------
    login.html 的内联脚本写入后，loginpage.js 的 stripHash() 会 getItem
    紧接着 removeItem 消费掉它。loginpage.js 是 type=module（defer），
    等探针 sleep 完早就跑过了，所以残留必然是 None —— 那是正确行为，
    不是没写进去。本轮据此误报过 4 个 FAIL。

    正解：用 Page.addScriptToEvaluateOnNewDocument 在文档创建前包装
    Storage.prototype.setItem，把写入记录下来（见 armed_page）。
    """
    return {
        "href": pg.js("location.href"),
        "pathname": pg.js("location.pathname"),
        "search": pg.js("location.search"),
        "hash": pg.js("location.hash"),
        # 拦截到的写入，而不是事后残留
        "writes": pg.js("JSON.stringify(window.__writes || [])"),
        "has_pw": pg.js("!!document.querySelector('#loginRoot input[type=password]')"),
    }


def armed_page() -> cdp.Page:
    """新 tab + 清 cookie + 埋 setItem 监听，返回可直接导航的 page。

    清 cookie 必须在导航之前：带着有效 session 访问 / 会拿到 200 主界面，
    走 index.html 的 JS 兜底守卫（href 变成 /login?next=%2F），
    测到的就不是服务端 302 那条路径了。
    """
    pg = fresh_page()
    pg.send("Page.enable")
    pg.send("Network.enable")
    pg.send("Network.clearBrowserCookies")
    pg.send("Page.addScriptToEvaluateOnNewDocument", source="""
      window.__writes = [];
      const _set = Storage.prototype.setItem;
      Storage.prototype.setItem = function (k, v) {
        try { window.__writes.push([k, v]); } catch (e) {}
        return _set.apply(this, arguments);
      };
    """)
    return pg


def carried_of(s: dict) -> str | None:
    """从拦截记录里取出 wb:carriedHash 的值。"""
    try:
        for k, v in json.loads(s.get("writes") or "[]"):
            if k == "wb:carriedHash":
                return v
    except (ValueError, TypeError):
        pass
    return None


print("=== 1) 未登录访问 /#/overview ===")
pg = armed_page()
try:
    pg.goto(f"{BASE}/#/overview")
    time.sleep(1.5)
    s = snap(pg)
    for k, v in s.items():
        print(f"    {k:9s} = {v!r}")
    check(s["pathname"] == "/login", "落在 /login", s["pathname"])
    check(s["hash"] == "", "地址栏 hash 已被抹掉（不再显露 #/overview）", s["hash"])
    check("%23" not in (s["search"] or ""), "hash 没被编码塞进 ?next=", s["search"])
    check(s["has_pw"], "登录表单已渲染")
    check(carried_of(s) == "#/overview",
          "落点已存进 sessionStorage（登录后能回去）", carried_of(s))
finally:
    pg.close()

print("\n=== 2) 换个 hash：/#/pool ===")
pg = armed_page()
try:
    pg.goto(f"{BASE}/#/pool")
    time.sleep(1.5)
    s = snap(pg)
    print(f"    href={s['href']!r}  carried={carried_of(s)!r}")
    check(s["pathname"] == "/login" and s["hash"] == "",
          "同样落在干净的 /login", f"{s['pathname']}{s['hash']}")
    check(carried_of(s) == "#/pool", "落点跟着变（不是写死的）", carried_of(s))
finally:
    pg.close()

print("\n=== 3) 无 hash 普通访问（回归）===")
pg = armed_page()
try:
    pg.goto(f"{BASE}/")
    time.sleep(1.5)
    s = snap(pg)
    print(f"    href={s['href']!r}  carried={carried_of(s)!r}")
    check(s["pathname"] == "/login" and s["hash"] == "",
          "落在 /login，无多余 hash")
    check(carried_of(s) in (None, ""), "没有凭空造出落点", carried_of(s))
finally:
    pg.close()

print("\n=== 4) 登录后是否回到原页面 ===")
pg = armed_page()
try:
    pg.goto(f"{BASE}/#/overview")
    time.sleep(1.5)
    before = snap(pg)
    print(f"    登录前: href={before['href']!r} carried={carried_of(before)!r}")

    # 填表提交
    pg.js("""(() => {
      const u = document.querySelector('#login-user');
      const p = document.querySelector('#login-pass');
      u.value = 'admin'; p.value = 'admin';
      u.dispatchEvent(new Event('input', {bubbles:true}));
      p.dispatchEvent(new Event('input', {bubbles:true}));
      document.querySelector('#loginRoot form').requestSubmit();
      return true;
    })()""")
    time.sleep(3.0)

    # 默认密码 admin 登录成功后必然撞「强制改密闸门」：
    # /api/auth/state 返回 must_change_password=true，loginpage.js 渲染改密
    # 表单并**停在 /login**。那是设计行为，不是 bug —— 所以这里必须先把密码
    # 改掉，才轮到验证跳转与落点。（本轮曾漏掉这步，误报 2 个 FAIL。）
    mid = pg.js("""JSON.stringify({
      href: location.href,
      gate: !!document.querySelector('#pw-old') && !!document.querySelector('#pw-new')
    })""")
    print(f"    登录后中间态: {mid}")

    # 默认密码 admin 登录后必然撞「强制改密闸门」，而且改密成功后后端会
    # **主动作废 session**：POST /api/auth/password 返回
    #   {"ok":true,"message":"密码已改，请重新登录","redirect":"/login"}
    # 随后 /api/auth/state 是 logged_in:false。所以改完密码停在 /login 是
    # 正确行为，必须再用新密码登录一次才轮到验证跳转与落点。
    # （本轮先漏了改密、又漏了这次重登，两次误报 FAIL。）
    if json.loads(mid)["gate"]:
        print("    -> 撞上强制改密闸门（预期），先改密")
        pg.js("""(() => {
          const o = document.querySelector('#pw-old');
          const n = document.querySelector('#pw-new');
          const n2 = document.querySelector('#pw-new2');
          o.value = 'admin'; n.value = 'hash-verify-pw-1'; n2.value = 'hash-verify-pw-1';
          for (const e of [o, n, n2]) e.dispatchEvent(new Event('input', {bubbles:true}));
          document.querySelector('#loginRoot form').requestSubmit();
          return true;
        })()""")
        time.sleep(3.5)
        st = pg.js("""(async () => {
          const r = await fetch('/api/auth/state', {credentials:'same-origin'});
          return await r.text();
        })()""")
        print(f"    改密后 state: {st}")

        # 关键：重新带着 hash 进来，这样落点才是这一轮登录要验的东西
        print("    -> 用新密码重新登录（带 #/overview 再进一次）")
        pg.send("Page.navigate", url=f"{BASE}/#/overview")
        time.sleep(2.5)
        relanded = snap(pg)
        print(f"    重进登录页: href={relanded['href']!r} "
              f"carried={carried_of(relanded)!r}")
        check(relanded["hash"] == "", "重进时地址栏依然干净", relanded["hash"])
        check(carried_of(relanded) == "#/overview",
              "重进时落点仍被记住", carried_of(relanded))

        pg.js("""(() => {
          const u = document.querySelector('#login-user');
          const p = document.querySelector('#login-pass');
          u.value = 'admin'; p.value = 'hash-verify-pw-1';
          for (const e of [u, p]) e.dispatchEvent(new Event('input', {bubbles:true}));
          document.querySelector('#loginRoot form').requestSubmit();
          return true;
        })()""")

    # 登录成功后 loginpage.js 会 location.replace(nextTarget())，
    # 导航会让当前 execution context 失效，正在 await 的 Runtime.evaluate
    # 抛 "Inspected target navigated or closed" —— 那是跳转成功的信号，
    # 不是失败。吞掉它并用 wall-clock 补等待，然后重连读最终状态。
    time.sleep(4.0)
    try:
        after = snap(pg)
    except RuntimeError as exc:
        print(f"    （导航中断连接，属跳转成功: {type(exc).__name__}）")
        time.sleep(1.5)
        pg = fresh_page()
        after = snap(pg)
    print(f"    最终: href={after['href']!r} hash={after['hash']!r}")
    check(after["pathname"] == "/", "已进主界面", after["pathname"])
    check(after["hash"] == "#/overview",
          "回到原本要去的 #/overview（落点没丢）", after["hash"])
finally:
    try:
        pg.close()
    except Exception:
        pass

print(f"\n=== 结果: {_pass} PASS / {_fail} FAIL ===")
raise SystemExit(1 if _fail else 0)
