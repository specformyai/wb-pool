#!/usr/bin/env python3
"""验证设置页「修改密码」真的修好了。

要证四件事
----------
  1. 请求 body 两套键都发（old/new + old_password/new_password）
     —— 只发 *_password 的部署后端读到空串，报「新密码至少 6 位」，
        文案完全指不到键名上，改密功能一直是坏的。
  2. 后端返回 200（不是 400）
  3. 改完不再留在设置页重载（session 已被后端作废，那样每个 /api/* 都 401），
     而是跳到 /login
  4. 新密码真的能登录 —— 这才证明密码确实改成功了，不是只看响应码

不能从外面看
------------
「改密失败」和「改密成功但没跳」从外面长得一模一样。唯一能区分的是把
fetch 包起来读真实 body 与响应（手法沿用 probe_changepw_wire.py）。

跑在本地临时实例上，不碰生产。
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

PW1 = "settings-verify-1"
PW2 = "settings-verify-2"

# 文档创建前注入：包 fetch 记请求/响应，记 console 错误
ARM = r"""
window.__calls = [];
window.__errs = [];
window.addEventListener('error', e => {
  try { window.__errs.push('onerror: ' + (e.message || '')); } catch (x) {}
});
window.addEventListener('unhandledrejection', e => {
  try { window.__errs.push('unhandled: ' + String(e.reason)); } catch (x) {}
});
const _ce = console.error;
console.error = function () {
  try { window.__errs.push('console.error: ' +
        Array.from(arguments).map(String).join(' ')); } catch (x) {}
  return _ce.apply(this, arguments);
};
// 记录必须写进 sessionStorage，不能只留在 window.__calls 上
// ------------------------------------------------------------------
// window.__calls 是 per-document 的。改密成功后 settings.js 会在 600ms 内
// location.replace('/login')，新文档重新执行这段注入脚本、把数组清成空，
// 所以「点击后 2 秒再读 window.__calls」必然读到 0 条 —— 看着像一次请求都
// 没发出，其实是发出了、成功了、然后连记录一起被导航冲掉了。本轮据此误判
// 过一次「前端自己拦下了请求」。sessionStorage 同源跨导航存活，才留得住。
const _KEY = 'wb:probeCalls';
function _rec(entry) {
  try {
    const cur = JSON.parse(sessionStorage.getItem(_KEY) || '[]');
    cur.push(entry);
    sessionStorage.setItem(_KEY, JSON.stringify(cur));
  } catch (e) {}
  try { window.__calls.push(entry); } catch (e) {}
}
const _fetch = window.fetch;
window.fetch = async function (input, init) {
  const url = (typeof input === 'string') ? input : (input && input.url) || '';
  const method = (init && init.method) || 'GET';
  let body = (init && init.body) || null;
  if (body && typeof body !== 'string') { try { body = String(body); } catch (e) { body = '<非字符串>'; } }
  let resp, txt = '', status = -1;
  try {
    resp = await _fetch.apply(this, arguments);
    status = resp.status;
    const clone = resp.clone();
    try { txt = (await clone.text()).slice(0, 400); } catch (e) { txt = '<读不出>'; }
    return resp;
  } catch (e) {
    txt = 'THROW: ' + String(e);
    throw e;
  } finally {
    _rec({ url, method, body, status, resp: txt });
  }
};
"""

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
    """开新 tab。Chrome 148 的 /json/new 只接受 PUT，老版本只认 GET。"""
    url = f"{cdp.CDP_HTTP}/json/new?about:blank"
    try:
        req = urllib.request.Request(url, method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            info = json.loads(r.read().decode())
    except urllib.error.HTTPError:
        with urllib.request.urlopen(url, timeout=10) as r:
            info = json.loads(r.read().decode())
    return cdp.Page(info["webSocketDebuggerUrl"])


def armed_page() -> cdp.Page:
    pg = fresh_page()
    pg.send("Page.enable")
    pg.send("Network.enable")
    pg.send("Network.clearBrowserCookies")
    pg.send("Page.addScriptToEvaluateOnNewDocument", source=ARM)
    return pg


def calls_of(pg: cdp.Page) -> list:
    """读取抓到的 fetch 记录。

    优先读 sessionStorage —— 它跨同源导航存活。改密成功后页面会
    location.replace('/login')，只读 window.__calls 会拿到新文档的空数组。
    """
    out: list = []
    for expr in ("sessionStorage.getItem('wb:probeCalls') || '[]'",
                 "JSON.stringify(window.__calls || [])"):
        try:
            got = json.loads(pg.js(expr) or "[]")
        except (RuntimeError, ValueError):
            continue
        for item in got:
            if item not in out:
                out.append(item)
    return out


def clear_calls(pg: cdp.Page) -> None:
    """清空两处记录，让后续断言只看这一步产生的请求。"""
    try:
        pg.js("window.__calls = []; "
              "sessionStorage.removeItem('wb:probeCalls'); true")
    except RuntimeError:
        pass


def settle(pg: cdp.Page, expr: str, secs: float = 0.0):
    """读页面状态；导航中断连接时返回 None（那是跳转成功，不是失败）。"""
    if secs:
        time.sleep(secs)
    try:
        return pg.js(expr)
    except RuntimeError as exc:
        print(f"    （导航中断: {type(exc).__name__}）")
        return None


def login(pg: cdp.Page, pw: str) -> None:
    pg.js("""(() => {
      const u = document.querySelector('#login-user');
      const p = document.querySelector('#login-pass');
      u.value = 'admin'; p.value = %s;
      for (const e of [u, p]) e.dispatchEvent(new Event('input', {bubbles:true}));
      document.querySelector('#loginRoot form').requestSubmit();
      return true;
    })()""" % json.dumps(pw))


pg = armed_page()
rc = 1
try:
    # ---------------------------------------------------------------- 前置
    print("=== 0) 前置：过掉强制改密闸门，进入 SPA ===")
    pg.send("Page.navigate", url=f"{BASE}/")
    time.sleep(2.0)
    login(pg, "admin")
    time.sleep(3.0)

    gate = pg.js("!!document.querySelector('#pw-old') && !!document.querySelector('#pw-new')")
    print(f"    默认密码闸门出现 = {gate}（预期 True，干净实例）")
    if gate:
        pg.js("""(() => {
          const o = document.querySelector('#pw-old');
          const n = document.querySelector('#pw-new');
          const n2 = document.querySelector('#pw-new2');
          o.value = 'admin'; n.value = %s; n2.value = %s;
          for (const e of [o, n, n2]) e.dispatchEvent(new Event('input', {bubbles:true}));
          document.querySelector('#loginRoot form').requestSubmit();
          return true;
        })()""" % (json.dumps(PW1), json.dumps(PW1)))
        time.sleep(3.5)
        # 闸门改密后后端作废 session，要用新密码重新登录
        pg.send("Page.navigate", url=f"{BASE}/")
        time.sleep(2.0)
        login(pg, PW1)
        settle(pg, "1", 4.0)

    href = settle(pg, "location.href")
    print(f"    当前 href = {href!r}")
    if href is None or "/login" in str(href):
        # 导航过，重连读
        pg = armed_page()
        pg.send("Page.navigate", url=f"{BASE}/")
        time.sleep(2.5)
        href = pg.js("location.href")
        print(f"    重连后 href = {href!r}")
    check("/login" not in str(href), "已进入 SPA（不在登录页）", href)

    # ---------------------------------------------------------------- 设置页
    print("\n=== 1) 打开设置页，点「修改密码」 ===")
    pg.send("Page.navigate", url=f"{BASE}/#/settings")
    time.sleep(3.0)
    st = pg.js("""JSON.stringify({
      hash: location.hash,
      btn: !!document.querySelector('[data-act="passwd"]'),
      label: (document.querySelector('[data-act="passwd"] span')||{}).textContent || ''
    })""")
    print(f"    设置页状态: {st}")
    s = json.loads(st)
    check(s["hash"] == "#/settings", "路由到 #/settings", s["hash"])
    check(s["btn"], "「修改密码」按钮已渲染")
    if not s["btn"]:
        raise SystemExit(1)

    pg.js("document.querySelector('[data-act=\"passwd\"]').click(); true")
    time.sleep(1.2)
    modal = pg.js("""JSON.stringify({
      box: !!document.querySelector('.modal-box'),
      old: !!document.querySelector('#pwOld'),
      new1: !!document.querySelector('#pwNew'),
      new2: !!document.querySelector('#pwNew2'),
      save: !!document.querySelector('[data-act="save"]')
    })""")
    print(f"    弹窗结构: {modal}")
    m = json.loads(modal)
    check(all(m.values()), "改密弹窗与三个输入框都在", modal)
    if not all(m.values()):
        raise SystemExit(1)

    # ---------------------------------------------------------------- 提交
    print("\n=== 2) 提交改密，抓真实请求 body ===")
    # 必须连 sessionStorage 一起清：step 0 过强制改密闸门时也 POST 过
    # /api/auth/password，只清 window.__calls 会让它残留下来被算成 2 次。
    clear_calls(pg)
    pg.js("""(() => {
      const o = document.querySelector('#pwOld');
      const a = document.querySelector('#pwNew');
      const b = document.querySelector('#pwNew2');
      o.value = %s; a.value = %s; b.value = %s;
      for (const e of [o, a, b]) e.dispatchEvent(new Event('input', {bubbles:true}));
      document.querySelector('[data-act="save"]').click();
      return true;
    })()""" % (json.dumps(PW1), json.dumps(PW2), json.dumps(PW2)))
    time.sleep(2.0)

    pw_calls = [c for c in calls_of(pg) if "/api/auth/password" in c["url"]]
    print(f"    捕获 /api/auth/password 调用: {len(pw_calls)} 次")
    for c in pw_calls:
        print(f"      {c['method']} -> {c['status']}")
        print(f"      body = {c['body']}")
        print(f"      resp = {c['resp']}")
    check(len(pw_calls) == 1, "发出了一次改密请求", len(pw_calls))
    if not pw_calls:
        raise SystemExit(1)

    call = pw_calls[0]
    body = json.loads(call["body"] or "{}")
    check(body.get("old") == PW1, "body 含 old（老后端读这个）", list(body))
    check(body.get("new") == PW2, "body 含 new（老后端读这个）", list(body))
    check(body.get("old_password") == PW1, "body 含 old_password（新后端兼容）", list(body))
    check(body.get("new_password") == PW2, "body 含 new_password（新后端兼容）", list(body))
    check(call["status"] == 200, "后端返回 200（不再是 400「新密码至少 6 位」）",
          f"{call['status']} {call['resp'][:120]}")

    # ---------------------------------------------------------------- 跳转
    print("\n=== 3) 改完是否跳登录页（而不是留在设置页 401）===")
    after = settle(pg, "JSON.stringify({href: location.href, hash: location.hash})", 3.0)
    if after is None:
        time.sleep(1.5)
        pg2 = fresh_page()
        after = pg2.js("JSON.stringify({href: location.href, hash: location.hash})")
        pg2.close()
    print(f"    最终: {after}")
    a = json.loads(after)
    check("/login" in a["href"], "已跳到 /login（session 已作废，不留在设置页）", a["href"])

    # ---------------------------------------------------------------- 新密码
    print("\n=== 4) 新密码真的生效（证明密码确实改了）===")
    pg3 = armed_page()
    try:
        pg3.send("Page.navigate", url=f"{BASE}/login")
        time.sleep(2.0)
        # 老密码应该被拒
        login(pg3, PW1)
        time.sleep(2.5)
        old_calls = [c for c in calls_of(pg3) if "/api/auth/login" in c["url"]]
        old_status = old_calls[-1]["status"] if old_calls else -1
        print(f"    老密码登录 -> {old_status}")
        check(old_status in (400, 401), "老密码已失效", old_status)

        # 新密码应该通
        clear_calls(pg3)
        login(pg3, PW2)
        time.sleep(3.0)
        new_calls = [c for c in calls_of(pg3) if "/api/auth/login" in c["url"]]
        new_status = new_calls[-1]["status"] if new_calls else -1
        print(f"    新密码登录 -> {new_status}")
        check(new_status == 200, "新密码能登录", new_status)

        errs = json.loads(pg3.js("JSON.stringify(window.__errs || [])") or "[]")
        check(not errs, "无 JS 运行时错误", errs[:2])
    finally:
        try:
            pg3.close()
        except Exception:  # noqa: BLE001
            pass

    rc = 1 if _fail else 0
finally:
    try:
        pg.close()
    except Exception:  # noqa: BLE001
        pass

print(f"\n=== 结果: {_pass} PASS / {_fail} FAIL ===")
raise SystemExit(rc)
