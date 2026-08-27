#!/usr/bin/env python3
"""浏览器里真跑一遍 /login，验证渲染而不是语法。

为什么必须真跑（Pitfall 19/21/28）
--------------------------------
node --check 通过 / 结构在 / 快照正常，都不代表内容对：
  * var(--不存在) 静默失效 → 卡片透明无圆角
  * import 了没导出的符号 → module 加载即抛，整页空白
  * 字段名错配 → 元素在但内容空
这些只有在运行时 DOM + console 错误里才看得见。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from cdp import Page   # noqa: E402

BASE = os.environ.get("WB_VERIFY_BASE", "http://127.0.0.1:8931")

_p = _f = 0


def check(cond, name, extra=""):
    global _p, _f
    if cond:
        _p += 1
        print(f"  PASS  {name}")
    else:
        _f += 1
        print(f"  FAIL  {name}  [{extra}]")


pg = Page()

# Chrome profile 会持久化 cookie：上一轮跑完留下的 wb_session 会让「首屏应该是
# 登录表单」这一步直接渲染成改密表单，看着像渲染坏了。每轮先清干净再测。
pg.send("Network.clearBrowserCookies")

# console 错误要在导航之前挂上，否则错过首屏那批
pg.send("Runtime.enable")
pg.send("Log.enable")

print("=== 1) /login 首屏真实渲染 ===")
pg.goto(f"{BASE}/login?nocache=1")

info = pg.js("""(() => {
  const q = s => document.querySelector(s);
  const card = q('.login-card');
  const cs = card ? getComputedStyle(card) : null;
  return JSON.stringify({
    url: location.pathname,
    title: document.title,
    bodyClass: document.body.className,
    hasOverlay: !!q('.login-overlay'),
    hasCard: !!card,
    // 卡片必须有真实底色和圆角 —— 变量名写错时这两个会是 transparent / 0px
    cardBg: cs ? cs.backgroundColor : '',
    cardRadius: cs ? cs.borderRadius : '',
    userInput: !!q('#login-user'),
    passInput: !!q('#login-pass'),
    submitText: (q('.login-submit') || {}).textContent || '',
    brandTitle: (q('.login-title') || {}).textContent || '',
    // 图标是否真渲染成 svg（lucide 没跑起来时只剩空 <i>）
    svgCount: document.querySelectorAll('svg').length,
    bodyTextLen: document.body.innerText.trim().length
  });
})()""")
d = json.loads(info)

check(d["url"] == "/login", "URL 停在 /login（不是遮罩式停在 /）", d["url"])
check(d["hasOverlay"], "登录层已渲染")
check(d["hasCard"], "登录卡片已渲染")
check(d["cardBg"] not in ("", "rgba(0, 0, 0, 0)", "transparent"),
      "卡片有真实底色（CSS 变量没写错）", d["cardBg"])
check(d["cardRadius"] not in ("", "0px"), "卡片有圆角", d["cardRadius"])
check(d["userInput"] and d["passInput"], "用户名/密码输入框都在")
check("登录" in d["submitText"], "提交按钮有文字", d["submitText"])
check("wb-pool" in d["brandTitle"], "品牌标题正确", d["brandTitle"])
check(d["bodyTextLen"] > 20, "页面有可见文字（不是白屏）", d["bodyTextLen"])

print("\n=== 2) 没有 JS 报错 ===")
errs = pg.js("""(() => JSON.stringify(window.__errs || []))()""")
# 上面拿不到历史错误，改用主动检查：module 有没有成功导出并执行
mod_ok = pg.js("""(async () => {
  try {
    const m = await import('/static/loginpage.js?probe=1');
    return typeof m.mountLoginPage === 'function' ? 'ok' : 'missing-export';
  } catch (e) { return 'import-failed: ' + e.message; }
})()""")
check(mod_ok == "ok", "loginpage.js 能被 import 且导出 mountLoginPage", mod_ok)

print("\n=== 3) 走一遍真实登录（默认密码 → 强制改密表单）===")
login_res = pg.js("""(async () => {
  const r = await fetch('/api/auth/login', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({user: 'admin', password: 'admin'})
  });
  return JSON.stringify({status: r.status, body: await r.json()});
})()""")
lr = json.loads(login_res)
check(lr["status"] == 200, "默认账号能登录", lr)
check(lr["body"].get("default_password") is True, "后端提示仍是默认密码")

# 重新加载 /login：已登录 + 默认密码 → 应停在改密表单，不该跳走
pg.goto(f"{BASE}/login?nocache=2")
pw = pg.js("""(() => {
  const q = s => document.querySelector(s);
  return JSON.stringify({
    url: location.pathname,
    hasPwForm: !!(q('#pw-old') && q('#pw-new') && q('#pw-new2')),
    heading: (q('.login-card h1, .login-card h3, .login-title') || {}).textContent || '',
    text: document.body.innerText.slice(0, 200)
  });
})()""")
p2 = json.loads(pw)
check(p2["url"] == "/login", "默认密码下仍停在 /login（没被放进主界面）", p2["url"])
check(p2["hasPwForm"], "强制改密表单已渲染", p2["text"][:120])

print("\n=== 4) 主界面 / 在默认密码下会被弹回 ===")
pg.goto(f"{BASE}/?nocache=3")
main = pg.js("""(() => JSON.stringify({
  url: location.pathname,
  search: location.search,
  hasSide: !!document.querySelector('#side .nav-item'),
  text: document.body.innerText.slice(0, 120)
}))()""")
m = json.loads(main)
check(m["url"] == "/login", "默认密码时访问 / 被送到 /login", f"{m['url']}{m['search']}")

pg.close()
print(f"\n{_p} passed, {_f} failed")
sys.exit(1 if _f else 0)
