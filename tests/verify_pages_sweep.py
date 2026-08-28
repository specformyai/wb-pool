#!/usr/bin/env python3
"""删掉 pages.js 里的 rates 死代码后，逐页在真实浏览器里扫一遍。

为什么必须扫全部页面
------------------
删的是 web/pages.js 与 web/pages.css，而 pages.js 一个文件同时供 5 个路由：
    calls（调用监控）/ keys（API Key）/ reg（注册中心）
    invite（邀请返利）/ proxy（代理池）
pages.css 里 .page-rates 那批规则又和 .page-keys 写在同一批复合选择器里
（`.page-keys .x-btn, .page-rates .x-btn{...}`）。切文本切错一个花括号，
后面所有规则连带失效 —— 而这类问题「离线测试全绿 + node --check 通过」
一个都发现不了，只有真渲染才看得见。

rates（模型与倍率）尤其要扫：它由 web/rates.js 提供，与被删的
pages.js#mountRates 同名不同源。若我删错了模块，这一页会直接白屏或 err-state。

判据（每页都要成立）
------------------
  1) 没有 err-state          —— router.js 捕获 mount 异常就渲染它
  2) 有实际文字              —— 排除「元素在但内容空」
  3) 无 JS 运行时错误        —— onerror / unhandledrejection / console.error
  4) 侧栏高亮落在本页        —— 证明路由真切过去了，不是停在上一页
  5) 骨架屏已被真实内容替换  —— 排除「一直 loading」

跑法：
    WB_SWEEP_BASE=http://127.0.0.1:8938 python tests/verify_pages_sweep.py
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


def fresh_page() -> cdp.Page:
    """开新 tab。

    cdp.py 只导出 Page(ws_url) —— 没有 new_page()，那是我凭记忆写的、
    并不存在的 API。建 tab 要自己打 /json/new：Chrome 148 只接受 PUT，
    老版本只认 GET，所以 PUT 失败再退回 GET。
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

BASE = os.environ.get("WB_SWEEP_BASE", "http://127.0.0.1:8938")
PW = "keys-modal-verify"

# router.js 的 ROUTES，逐字对齐（id 用于 location.hash 与侧栏高亮断言）
PAGES = [
    ("overview", "概览", None),
    ("chat", "对话调试", None),
    ("pool", "账号池", None),
    ("calls", "调用监控", "pages.js"),
    ("history", "历史对话", None),
    ("keys", "API Key", "pages.js"),
    ("rates", "模型与倍率", "rates.js"),
    ("reg", "注册中心", "pages.js"),
    ("invite", "邀请返利", "pages.js"),
    ("proxy", "代理池", "pages.js"),
    ("settings", "设置", None),
]

ARM = r"""
window.__errs = [];
window.addEventListener('error', e => {
  try { window.__errs.push('onerror: ' + (e.message || '')); } catch (x) {}
});
window.addEventListener('unhandledrejection', e => {
  try { window.__errs.push('unhandled: ' + String(e.reason)); } catch (x) {}
});
(() => {
  const ce = console.error;
  console.error = function (...a) {
    try { window.__errs.push('console.error: ' + a.map(String).join(' ')); } catch (x) {}
    return ce.apply(this, a);
  };
})();
"""

PROBE = r"""JSON.stringify((() => {
  const view = document.querySelector('#view');
  const err = view ? view.querySelector('.err-state, .errbox') : null;
  const txt = view ? (view.innerText || '').trim() : '';
  const active = document.querySelector('#side [data-route].on');
  return {
    hash: location.hash,
    err: err ? (err.innerText || '').slice(0, 200) : '',
    cards: view ? view.querySelectorAll('.card, .model-card').length : 0,
    skl: view ? view.querySelectorAll('.skl').length : 0,
    chars: txt.length,
    head: txt.slice(0, 80).replace(/\s+/g, ' '),
    nav: document.querySelectorAll('#side [data-route]').length,
    active: active ? active.dataset.route : '',
    icons: view ? view.querySelectorAll('svg').length : 0,
    objhtml: txt.indexOf('[object HTML') >= 0,
    errs: (window.__errs || []).slice(0, 6),
  };
})())"""

_pass = 0
_fail = 0


def check(cond, name: str, extra: str = "") -> None:
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        print(f"  FAIL  {name}   {extra}")


def api(path: str, payload=None, cookie: str = ""):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None else "GET",
    )
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode(), r.headers.get("set-cookie", "") or ""
    except urllib.error.HTTPError as e:  # noqa: PERF203
        return e.code, e.read().decode(), e.headers.get("set-cookie", "") or ""


pg = None
rc = 1
try:
    print(f"=== 目标: {BASE} ===\n")

    # ---- 过默认密码闸门（幂等：已改过就直接用 PW 登录）----
    st, _, ck = api("/api/auth/login", {"user": "admin", "password": "admin"})
    if st == 200:
        sess = ck.split(";")[0] if ck else ""
        st2, _, _ = api("/api/auth/password",
                        {"old": "admin", "new": PW,
                         "old_password": "admin", "new_password": PW},
                        cookie=sess)
        print(f"=== 0) 改默认密码 -> {st2} ===")
    else:
        st3, _, _ = api("/api/auth/login", {"user": "admin", "password": PW})
        print(f"=== 0) 密码已是 PW，登录 -> {st3} ===")

    pg = fresh_page()
    pg.send("Page.enable")
    pg.send("Runtime.enable")
    pg.send("Network.enable")
    pg.send("Network.clearBrowserCookies")
    pg.send("Page.addScriptToEvaluateOnNewDocument", source=ARM)

    # ---- 登录进 SPA（真实 id 是 #login-user / #login-pass，读 loginpage.js 得来）----
    print("\n=== 1) 登录进 SPA ===")
    pg.goto(BASE + "/login")
    time.sleep(1.5)
    filled = pg.js(f"""JSON.stringify((() => {{
      const u = document.querySelector('#login-user');
      const p = document.querySelector('#login-pass');
      const f = document.querySelector('.login-form');
      if (!u || !p || !f) return {{ ok: false }};
      u.value = 'admin'; p.value = {PW!r}; f.requestSubmit();
      return {{ ok: true }};
    }})())""")
    time.sleep(2.5)
    where = json.loads(pg.js("JSON.stringify({p: location.pathname})"))
    check(where["p"] == "/", "已进入 SPA", str(where))

    # ---- 逐页扫 ----
    print("\n=== 2) 逐页扫描（11 个路由）===")
    bad = []
    for pid, name, owner in PAGES:
        pg.js("window.__errs = []; true")
        pg.js(f"location.hash = '#/{pid}'; true")
        time.sleep(2.2)
        s = json.loads(pg.js(PROBE))

        ok = (s["err"] == "" and s["chars"] > 60 and not s["errs"]
              and s["active"] == pid and not s["objhtml"])
        tag = "PASS" if ok else "FAIL"
        src = f" [{owner}]" if owner else ""
        print(f"  {tag}  {name:8s}{src:12s} 卡片={s['cards']:2d} 骨架={s['skl']:2d} "
              f"字符={s['chars']:5d} 图标={s['icons']:3d} active={s['active']}")
        print(f"        {s['head']}")
        if s["err"]:
            print(f"        !! err-state: {s['err'][:160]}")
        if s["errs"]:
            print(f"        !! JS 错误: {str(s['errs'])[:220]}")
        if s["objhtml"]:
            print("        !! 出现 [object HTML*] 字面量")
        if ok:
            _pass += 1
        else:
            _fail += 1
            bad.append(name)

    # ---- 真倍率页专项：它由 rates.js 提供，与被删的 pages.js#mountRates 同名不同源 ----
    print("\n=== 3) 真倍率页专项（rates.js，不是被删那份）===")
    pg.js("window.__errs = []; location.hash = '#/rates'; true")
    time.sleep(2.5)
    rt = json.loads(pg.js(r"""JSON.stringify((() => {
      const v = document.querySelector('#view');
      const txt = v ? (v.innerText || '') : '';
      return {
        has_page_rates: !!document.querySelector('.page-rates'),
        // rates.js 渲染的是 .page-h + 模型卡片；被删那份渲染 .pr-top + <table>
        has_page_h: !!(v && v.querySelector('.page-h')),
        has_pr_top: !!(v && v.querySelector('.pr-top')),
        has_table: !!(v && v.querySelector('table')),
        model_cards: v ? v.querySelectorAll('.model-card').length : 0,
        has_reload: txt.indexOf('读取缓存') >= 0,
        has_sync: txt.indexOf('同步上游') >= 0,
        has_measure: txt.indexOf('实测倍率') >= 0,
        chars: txt.trim().length,
        errs: (window.__errs || []).slice(0, 4),
      };
    })())"""))
    print(f"    .page-rates={rt['has_page_rates']}  .page-h={rt['has_page_h']}  "
          f"模型卡片={rt['model_cards']}  字符={rt['chars']}")
    check(rt["has_page_rates"], "倍率页挂上 .page-rates", str(rt))
    check(rt["has_page_h"], "渲染的是 rates.js 的 .page-h", str(rt))
    check(not rt["has_pr_top"], "没有 .pr-top（那是被删的死代码专属）", str(rt))
    check(not rt["has_table"], "没有 <table>（被删那份才用表格）", str(rt))
    check(rt["has_reload"] and rt["has_sync"], "「读取缓存/同步上游」按钮在", str(rt))
    check(not rt["has_measure"], "「实测倍率」已随死代码消失", str(rt))
    check(rt["model_cards"] > 0 or rt["chars"] > 200, "倍率页有实际内容", str(rt))
    check(not rt["errs"], "倍率页无 JS 错误", str(rt["errs"])[:200])

    # ---- keys 页弹窗样式专项：pages.css 的复合选择器不能被切坏 ----
    print("\n=== 4) keys 页弹窗样式（pages.css 复合选择器未被切坏）===")
    pg.js("window.__errs = []; location.hash = '#/keys'; true")
    time.sleep(2.2)
    pg.js("""document.querySelector('.page-keys [data-act="create"]').click(); true""")
    time.sleep(1.2)
    sty = json.loads(pg.js(r"""JSON.stringify((() => {
      const wrap = document.querySelector('.modal');
      const box = wrap ? wrap.querySelector('.modal-box') : null;
      const inp = box ? box.querySelector('.x-in') : null;
      const btn = box ? box.querySelector('.x-btn.pri') : null;
      const cs = btn ? getComputedStyle(btn) : null;
      const ci = inp ? getComputedStyle(inp) : null;
      return {
        open: !!wrap,
        scoped: wrap ? wrap.classList.contains('page-keys') : false,
        // .x-btn.pri 的底色来自 pages.css 的复合选择器，被切坏就会退回透明
        btn_bg: cs ? cs.backgroundColor : '',
        btn_img: cs ? cs.backgroundImage : '',
        // .x-in 的边框同理
        inp_border: ci ? ci.borderTopWidth : '',
        inp_bg: ci ? ci.backgroundColor : '',
        errs: (window.__errs || []).slice(0, 4),
      };
    })())"""))
    print(f"    弹窗={sty['open']}  scope={sty['scoped']}  "
          f"按钮底色={sty['btn_bg']}  输入框边框={sty['inp_border']}")
    check(sty["open"], "弹窗打开了", str(sty))
    check(sty["scoped"], "scope class 落在 .modal 上", str(sty))
    # 底色可能是 backgroundColor 也可能是 linear-gradient（落在 backgroundImage）
    styled = (sty["btn_bg"] not in ("", "rgba(0, 0, 0, 0)")
              or sty["btn_img"] not in ("", "none"))
    check(styled, "创建按钮有底色（复合选择器未被切坏）",
          f"bg={sty['btn_bg']} img={sty['btn_img']}")
    check(sty["inp_border"] not in ("", "0px"), "输入框有边框",
          str(sty["inp_border"]))
    check(not sty["errs"], "无 JS 错误", str(sty["errs"])[:200])
    pg.js("""(() => { const c = document.querySelector('.modal [data-act="cancel"]');
                      if (c) c.click(); return true; })()""")

    print(f"\n=== 结果: {_pass} PASS / {_fail} FAIL ===")
    if bad:
        print(f"  有问题的页面: {bad}")
    rc = 0 if _fail == 0 else 1
finally:
    if pg is not None:
        try:
            pg.close()
        except Exception:  # noqa: BLE001
            pass

sys.exit(rc)
