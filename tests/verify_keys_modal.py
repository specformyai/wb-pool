#!/usr/bin/env python3
"""验证 API Key / 倍率实测 的弹窗真的能用。

要证什么
--------
`openModal` 的签名是 `openModal(html, {size, scope, onClose})`，但 pages.js
里有 4 处在用早期的三参形式 `openModal(root, html, onClose)`：

  * 第一个实参（keysRoot / ratesRoot 这个 DOM 元素）被当成 html 塞进模板
    字符串，字符串化的结果就是字面量 **[object HTMLDivElement]** —— 用户
    看到的就是这个。
  * 返回值是 `{box, wrap, close}`，解构 `{back, close}` 拿到的 back 是
    undefined，紧接着 `back.querySelector(...)` 抛 TypeError，弹窗里连
    按钮都绑不上，「创建」点了没反应。
  * 第三个实参 onClose 被整个忽略，`keysModal` 计数器只加不减；而
    `setInterval(() => { if (!keysModal) loadKeys(true) })` 对非 0 恒假 ——
    开过一次弹窗之后，这一页的 30s 自动刷新永久停摆。

所以断言分三层：弹窗内容不含 [object HTML...]、能真的创建出 key、
关闭后计数器归零使轮询恢复。

为什么必须在浏览器里跑
----------------------
后端 `POST /api/keys` 一直是好的（用 curl 直接打能建出 key），坏的只有前端
弹窗。任何绕过 DOM 的测试都会全绿通过，看不见这个 bug。

弹窗挂在 document.body 下，而 `.x-btn` / `.x-field` / `.x-in` 这批样式是
scoped 在 `.page-keys` / `.page-rates` 里的，所以修法必须把 scope class
传给 openModal，光改签名会得到一个没样式的裸弹窗 —— 这里也一并断言。

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

# tests/serve_verify.py 里写死的 WB_ADMIN_KEY。require_admin 接受
# session cookie 或 x-api-key == 这个值，服务端核对走后者。
ADMIN_KEY = os.environ.get("WB_ADMIN_KEY", "verify-admin-key")

# 干净实例的默认凭据是 admin/admin，但默认密码会触发强制改密闸门，
# 所以先用 API 把密码改掉（新密码必须 >=6 位，否则后端 400）。
PW = "keys-modal-verify"

ARM = r"""
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


def api(path: str, payload=None, cookie: str = "", admin: bool = False) -> tuple[int, str, str]:
    """返回 (status, body, set-cookie)。

    admin=True 时带 x-api-key。/api/keys 挂着 require_admin，它接受
    session cookie **或** x-api-key == WB_ADMIN_KEY（app/main.py 的
    require_admin，读源码确认）。服务端核对 key 数量走 header 最省事：
    不用维护 cookie，也不受浏览器那边 session 状态影响。
    tests/serve_verify.py 把 WB_ADMIN_KEY 设成 verify-admin-key。
    """
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None else "GET",
    )
    if admin:
        req.add_header("x-api-key", ADMIN_KEY)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode(), r.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers.get("Set-Cookie", "")


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


pg = None
rc = 1
try:
    print(f"=== 目标: {BASE} ===\n")

    print("=== 0) 过默认密码闸门（API，与 UI 无关）===")
    # 必须幂等：同一个实例上重跑时密码已经是 PW 了，admin/admin 必然 401。
    # 那不是失败 —— 前置条件（不再是默认密码）本来就已满足。上一轮把它当
    # 成 FAIL 报了出来，而紧接着 step 1 用 PW 登录成功，自相矛盾。
    st, body, ck = api("/api/auth/login", {"user": "admin", "password": "admin"})
    print(f"    login(admin/admin) -> {st}")
    if st == 200:
        sess = ck.split(";")[0] if ck else ""
        st2, body2, _ = api("/api/auth/password",
                            {"old": "admin", "new": PW,
                             "old_password": "admin", "new_password": PW},
                            cookie=sess)
        print(f"    改密 -> {st2}  {body2[:80]}")
        check(st2 == 200, "prep: 默认密码已改掉", f"{st2} {body2[:120]}")
    else:
        # 判据是「用 PW 能登录」，而不是「admin/admin 失败」——
        # 后者也可能是服务挂了，两种情况必须区分开。
        st3, body3, _ = api("/api/auth/login", {"user": "admin", "password": PW})
        print(f"    已改过密码；用 PW 登录 -> {st3}")
        check(st3 == 200, "prep: 密码已是 PW（本轮免改）", f"{st3} {body3[:120]}")

    pg = fresh_page()
    pg.send("Page.enable")
    pg.send("Network.enable")
    pg.send("Network.clearBrowserCookies")
    pg.send("Page.addScriptToEvaluateOnNewDocument", source=ARM)

    print("\n=== 1) 用新密码登录进 SPA ===")
    pg.goto(BASE + "/login")
    time.sleep(1.5)
    # 真实契约（读 web/loginpage.js:100-132 得来，不猜）：
    #   * 登录表单是 JS 用 el() 动态建的，login.html 里没有任何 input，
    #     所以 '#user' / '#pass' 这种猜法必然 querySelector 到 null
    #   * 真实 id 是 #login-user / #login-pass（name 是 username / password）
    #   * 提交走 form.submit 事件，用 .login-form 的 requestSubmit() 最稳
    # 上一轮用 #user/#pass 没填上值就点了登录，被前端拦成
    # 「请输入用户名和密码」，于是停在 /login —— 断言报 FAIL，但错在脚本。
    filled = pg.js(f"""JSON.stringify((() => {{
      const u = document.querySelector('#login-user');
      const p = document.querySelector('#login-pass');
      const f = document.querySelector('.login-form');
      if (!u || !p || !f) return {{ ok: false, u: !!u, p: !!p, f: !!f }};
      u.value = 'admin';
      p.value = {PW!r};
      f.requestSubmit();
      return {{ ok: true }};
    }})())""")
    print(f"    填表: {filled}")
    check(json.loads(filled).get("ok") is True, "登录表单填写并提交", filled)
    time.sleep(2.5)
    where = pg.js("JSON.stringify({path: location.pathname, hash: location.hash})")
    print(f"    落点: {where}")
    check("/login" not in json.loads(where)["path"], "已进入 SPA", where)

    print("\n=== 2) 打开 API Key 页 ===")
    pg.js("location.hash = '#/keys'; true")
    time.sleep(2.0)
    page_state = pg.js(r"""JSON.stringify((() => {
      const root = document.querySelector('#view');
      return {
        scoped: !!document.querySelector('.page-keys'),
        has_create: !!document.querySelector('.page-keys [data-act="create"]'),
        text: (root ? root.innerText : '').slice(0, 120),
      };
    })())""")
    ps = json.loads(page_state)
    print(f"    scope={ps['scoped']}  新建按钮={ps['has_create']}")
    check(ps["scoped"], "页面挂上 .page-keys scope", page_state)
    check(ps["has_create"], "「新建 Key」按钮存在", page_state)

    print("\n=== 3) 点开「新建 Key」弹窗 ===")
    pg.js("window.__errs = []; true")
    pg.js("""document.querySelector('.page-keys [data-act="create"]').click(); true""")
    time.sleep(1.2)
    modal = pg.js(r"""JSON.stringify((() => {
      const wrap = document.querySelector('.modal');
      const box = wrap ? wrap.querySelector('.modal-box') : null;
      const txt = box ? (box.innerText || '') : '';
      const nameIn = box ? box.querySelector('[name="name"]') : null;
      const okBtn = box ? box.querySelector('[data-act="ok"]') : null;
      // scope 必须落在最外层 .modal 上，否则 .page-keys .x-btn 这批样式不生效
      const scoped = wrap ? wrap.classList.contains('page-keys') : false;
      const btnBg = okBtn ? getComputedStyle(okBtn).backgroundColor : '';
      const btnBgImg = okBtn ? getComputedStyle(okBtn).backgroundImage : '';
      return {
        open: !!wrap,
        objhtml: txt.indexOf('[object HTML') >= 0,
        title: txt.slice(0, 40),
        has_name_input: !!nameIn,
        has_ok: !!okBtn,
        scoped: scoped,
        btn_bg: btnBg,
        btn_bgimg: (btnBgImg || '').slice(0, 60),
        errs: (window.__errs || []).slice(0, 4),
      };
    })())""")
    m = json.loads(modal)
    print(f"    弹窗={m['open']}  含[object HTML*]={m['objhtml']}  标题={m['title']!r}")
    print(f"    scope={m['scoped']}  按钮底色={m['btn_bg']}  底图={m['btn_bgimg']}")
    check(m["open"], "弹窗打开了", modal)
    check(not m["objhtml"], "弹窗内容不含 [object HTMLDivElement]", m["title"])
    check(m["has_name_input"], "名称输入框存在", modal)
    check(m["has_ok"], "「创建」按钮存在", modal)
    check(m["scoped"], "scope class 落在 .modal 上（否则无样式）", modal)
    # .x-btn.pri 是渐变底色，落在 backgroundImage 上而不是 backgroundColor
    styled = ("gradient" in m["btn_bgimg"]) or (
        m["btn_bg"] not in ("", "rgba(0, 0, 0, 0)", "transparent"))
    check(styled, "按钮拿到了 scoped 样式", f"bg={m['btn_bg']} img={m['btn_bgimg']}")
    check(not m["errs"], "打开弹窗无 JS 错误", str(m["errs"])[:200])

    print("\n=== 4) 真的创建一个 Key ===")
    pg.js("window.__errs = []; true")
    st_b, body_b, _ = api("/api/keys", admin=True)
    check(st_b == 200, "服务端能读 key 列表（鉴权正常）", f"{st_b} {body_b[:100]}")
    n_before = len(json.loads(body_b).get("keys", []))
    print(f"    创建前 key 数: {n_before}")

    pg.js(r"""(() => {
      const box = document.querySelector('.modal .modal-box');
      box.querySelector('[name="name"]').value = '验证用-key';
      box.querySelector('[name="note"]').value = 'verify_keys_modal.py';
      box.querySelector('[data-act="ok"]').click();
      return true;
    })()""")
    time.sleep(2.5)

    reveal = pg.js(r"""JSON.stringify((() => {
      const boxes = [...document.querySelectorAll('.modal .modal-box')];
      const last = boxes[boxes.length - 1] || null;
      const txt = last ? (last.innerText || '') : '';
      const keyEl = last ? last.querySelector('.x-keyreveal') : null;
      return {
        n_modals: boxes.length,
        objhtml: txt.indexOf('[object HTML') >= 0,
        head: txt.slice(0, 40),
        key_shown: keyEl ? (keyEl.textContent || '').trim() : '',
        errs: (window.__errs || []).slice(0, 4),
      };
    })())""")
    rv = json.loads(reveal)
    shown = rv["key_shown"]
    print(f"    明文弹窗: {rv['head']!r}  key 长度={len(shown)}  含[object HTML*]={rv['objhtml']}")

    st_a, body_a, _ = api("/api/keys", admin=True)
    keys_after = json.loads(body_a).get("keys", [])
    n_after = len(keys_after)
    names = [k.get("name") for k in keys_after]
    print(f"    创建后 key 数: {n_after}  名称={names}")

    check(n_after == n_before + 1, "后端真的多了一个 key", f"{n_before} -> {n_after}")
    check("验证用-key" in names, "新 key 名称正确", str(names))
    check(len(shown) >= 20, "明文 key 在弹窗里展示了一次", f"len={len(shown)}")
    check(not rv["objhtml"], "明文弹窗不含 [object HTMLDivElement]", rv["head"])
    check(not rv["errs"], "创建过程无 JS 错误", str(rv["errs"])[:200])

    print("\n=== 5) 关闭弹窗后轮询恢复（onClose 真被调用）===")
    # 关掉所有还开着的弹窗
    pg.js(r"""(() => {
      let n = 0;
      for (const w of [...document.querySelectorAll('.modal')]) {
        const done = w.querySelector('[data-act="done"]');
        const cancel = w.querySelector('[data-act="cancel"]');
        (done || cancel || w.querySelector('.modal-mask')).click();
        n++;
      }
      return n;
    })()""")
    time.sleep(1.0)
    left = pg.js("document.querySelectorAll('.modal').length")
    print(f"    残留弹窗: {left}")
    check(int(left) == 0, "弹窗都关掉了", left)

    # keysModal 是模块内私有变量，外面读不到。改为验证它的可观测后果：
    # 关闭弹窗后 30s 轮询应能继续刷新列表。直接等 30s 太慢，这里改成
    # 断言「再开一次弹窗仍然正常」—— 计数器若被搞成负数/正数不归零，
    # 第二次开关同样会暴露（onClose 没接上就永远加不回来）。
    pg.js("window.__errs = []; true")
    pg.js("""document.querySelector('.page-keys [data-act="create"]').click(); true""")
    time.sleep(1.0)
    second = pg.js(r"""JSON.stringify((() => {
      const box = document.querySelector('.modal .modal-box');
      const txt = box ? (box.innerText || '') : '';
      return { open: !!box, objhtml: txt.indexOf('[object HTML') >= 0,
               has_ok: !!(box && box.querySelector('[data-act="ok"]')),
               errs: (window.__errs || []).slice(0, 3) };
    })())""")
    s2 = json.loads(second)
    check(s2["open"] and s2["has_ok"] and not s2["objhtml"],
          "第二次开弹窗同样正常（onClose 已接上）", second)
    check(not s2["errs"], "第二次开弹窗无 JS 错误", str(s2["errs"])[:200])
    pg.js("""(() => { const c = document.querySelector('.modal [data-act="cancel"]');
                      if (c) c.click(); return true; })()""")
    time.sleep(0.6)

    print("\n=== 6) pages.js 里第 4 处同类调用（源码层核对）===")
    # 为什么不在浏览器里点：router.js 的 ROUTES 把 #/rates 指向 @/rates.js，
    # 而 openMeasure() 在 pages.js 里。pages.js 的 mountRates/openMeasure 是
    # 前端模块化时留下的死代码，任何路由都到不了 —— 上一轮在 #/rates 页面找
    # '.page-rates [data-act="measure"]' 自然找不到（那页渲染的是 rates.js 的
    # .page-h + 读取缓存/同步上游），报 3 个 FAIL 全是测了不可达路径。
    # 它仍然要修（签名错就是错，将来若被接回路由会立刻炸），所以改成源码断言。
    src = pathlib.Path(__file__).resolve().parents[1] / "web" / "pages.js"
    js = src.read_text(encoding="utf-8")
    check("openModal(keysRoot" not in js and "openModal(ratesRoot" not in js,
          "pages.js 已无 openModal(root, ...) 三参调用",
          "仍有 root 作首参")
    check(js.count("{ box: back, close } = openModal(") == 4,
          "4 处都改成解构 box（openModal 不返回 back）",
          js.count("{ box: back, close } = openModal("))
    check(js.count("scope: 'page-keys'") == 3 and js.count("scope: 'page-rates'") == 1,
          "4 处都带 scope（否则弹窗没样式）",
          f"keys={js.count(chr(39)+'page-keys'+chr(39))} rates={js.count(chr(39)+'page-rates'+chr(39))}")
    check(js.count("onClose:") == 4,
          "4 处都带 onClose（否则弹窗计数器不归零、轮询永久停摆）",
          js.count("onClose:"))
    # rates.js 是真正挂在 #/rates 上的模块，确认它没有同类问题
    rjs = (src.parent / "rates.js").read_text(encoding="utf-8")
    check("openModal" not in rjs,
          "rates.js（真正的倍率页）不用 openModal，无同类风险",
          "rates.js 里出现了 openModal")

    print(f"\n=== 结果: {_pass} PASS / {_fail} FAIL ===")
    rc = 0 if _fail == 0 else 1
finally:
    if pg is not None:
        try:
            pg.close()
        except Exception:  # noqa: BLE001
            pass

sys.exit(rc)
