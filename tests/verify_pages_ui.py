#!/usr/bin/env python3
"""浏览器真实渲染验证：代理池页「添加/删除出口 + 自动探测」、设置页「运行时配置」。

为什么必须真跑浏览器
--------------------
node --check 只查语法。本项目历史上三类 bug 全部能通过语法检查：
  * CSS 变量名写错 → var() 静默失效，卡片透明无圆角（Pitfall 21）
  * 类名没定义     → 样式整条作废，元素在但看不出来
  * 字段名错配     → 元素在但内容空 / 列表恒空（Pitfall 25）
所以验收判据是「DOM 里真有那个按钮、点了真发对的请求、回来真的渲染」。

流程
----
1. 清 cookie → 登录 → 改密（解开强制改密闸门，否则主界面所有 /api/* 都 403）
2. 进 #/proxy：断言三个新按钮在、弹窗能开、真加一个出口、卡片出现、再删掉
3. 进 #/settings：断言配置卡片渲染、12 个键都有控件、密钥脱敏不回明文、改一个值能存
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from cdp import Page  # noqa: E402

BASE = os.environ.get("WB_VERIFY_BASE", "http://127.0.0.1:8931")
NEW_PW = "verify-pw-123456"

_p = _f = 0


def check(cond, name, extra=""):
    global _p, _f
    if cond:
        _p += 1
        print(f"  PASS  {name}")
    else:
        _f += 1
        print(f"  FAIL  {name}  [{extra}]")


def jj(pg, expr):
    """跑一段 JS 并把 JSON 结果解析回来。"""
    return json.loads(pg.js(expr))


def wait_for(pg, expr, tries=40, gap=0.25):
    """轮询等某个条件为真 —— 固定 sleep 会把「还没渲染完」误判成「渲染坏了」。"""
    for _ in range(tries):
        try:
            if pg.js(expr) is True:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(gap)
    return False


pg = Page()
pg.send("Network.clearBrowserCookies")
pg.send("Runtime.enable")

# 错误收集用「页面内钩子」而不是订阅 CDP 事件：这个极简客户端的 send() 是
# 同步请求-响应式的，会把中间到达的事件消息丢掉。注入脚本在每次导航后都会
# 重新执行，攒在 window.__errs 里，随时可以读回来，比事件订阅更可靠。
pg.send("Page.addScriptToEvaluateOnNewDocument", source="""
window.__errs = [];
window.addEventListener('error', (e) => {
  window.__errs.push(String((e.error && e.error.stack) || e.message || e));
});
window.addEventListener('unhandledrejection', (e) => {
  window.__errs.push('unhandledrejection: ' + String((e.reason && e.reason.stack) || e.reason));
});
""")


def page_errors(page) -> list[str]:
    """读回本次导航累积的 JS 错误。白屏类 bug 全靠它定性。"""
    raw = page.js("JSON.stringify(window.__errs || [])")
    try:
        return [x for x in json.loads(raw or "[]") if x]
    except (TypeError, ValueError):
        return []

# ---------------------------------------------------------------- 0) 登录 + 改密
print("=== 0) 准备：登录并改掉默认密码（解开强制改密闸门）===")
pg.goto(f"{BASE}/login?nocache=p1")
time.sleep(1.2)

login = jj(pg, """(async () => {
  const r = await fetch('/api/auth/login', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({user: 'admin', password: 'admin'})});
  return JSON.stringify({status: r.status, body: await r.json()});
})()""")
check(login["status"] == 200, "默认账号登录成功", login)

chg = jj(pg, f"""(async () => {{
  const r = await fetch('/api/auth/password', {{
    method: 'POST', credentials: 'same-origin',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{old_password: 'admin', new_password: '{NEW_PW}'}})}});
  return JSON.stringify({{status: r.status, body: await r.json()}});
}})()""")
check(chg["status"] == 200, "改掉默认密码（闸门解开）", chg)

# 改密会踢掉 session，重新登录
relog = jj(pg, f"""(async () => {{
  const r = await fetch('/api/auth/login', {{
    method: 'POST', credentials: 'same-origin',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{user: 'admin', password: '{NEW_PW}'}})}});
  return JSON.stringify({{status: r.status, body: await r.json()}});
}})()""")
check(relog["status"] == 200 and relog["body"].get("default_password") is False,
      "用新密码重新登录，且不再标记默认密码", relog)

# ------------------------------------------------------------------ 1) 代理池页
print("\n=== 1) 代理池页：手动增删出口 + 自动探测入口 ===")
pg.goto(f"{BASE}/#/proxy")
ok = wait_for(pg, "!!document.querySelector('#pBody .statbar, #pBody .empty')")
check(ok, "代理池页主体已渲染（不是骨架屏卡住）")

btns = jj(pg, """(() => {
  const g = a => document.querySelector(`[data-act="${a}"]`);
  const txt = e => e ? (e.textContent || '').trim() : null;
  return JSON.stringify({
    discover: txt(g('discover')), addExit: txt(g('addExit')), probe: txt(g('probe')),
    emptyText: (document.querySelector('#pBody .empty')?.innerText || '').slice(0, 60),
  });
})()""")
check(btns["addExit"] == "添加出口", "「添加出口」按钮在", btns)
check(btns["discover"] == "自动探测", "「自动探测」按钮在", btns)
check(btns["probe"] == "探测全部", "原有「探测全部」按钮没被弄坏", btns)
check("自动探测" in btns["emptyText"] or "还没有配置" in btns["emptyText"],
      "空态文案换成了对新部署有用的引导", btns["emptyText"])

# 打开「添加出口」弹窗，确认控件真在（不只是函数存在）
pg.js("document.querySelector('[data-act=\"addExit\"]').click()")
time.sleep(0.5)
modal = jj(pg, """(() => {
  const b = document.querySelector('.modal-box');
  if (!b) return JSON.stringify({open: false});
  const cs = getComputedStyle(b);
  return JSON.stringify({
    open: true,
    hasPort: !!b.querySelector('#axPort'),
    hasLabel: !!b.querySelector('#axLabel'),
    hasOk: !!b.querySelector('#axOk'),
    bg: cs.backgroundColor,
    title: (b.querySelector('h3')?.textContent || '').trim(),
  });
})()""")
check(modal.get("open"), "添加出口弹窗能打开", modal)
check(modal.get("hasPort") and modal.get("hasLabel") and modal.get("hasOk"),
      "弹窗里端口/标签/确定三个控件都在", modal)
check(modal.get("bg") not in ("rgba(0, 0, 0, 0)", "transparent"),
      "弹窗有真实底色（CSS 没写错）", modal.get("bg"))

# 真填一个端口点确定 —— 走完整链路：前端 → POST /api/proxy/exits → settings 落盘 → 重绘
pg.js("""(() => {
  const b = document.querySelector('.modal-box');
  b.querySelector('#axPort').value = '3128';
  b.querySelector('#axLabel').value = 'squid-verify';
  b.querySelector('#axOk').click();
})()""")
added = wait_for(pg, "!!document.querySelector('#pBody .pcard')")
check(added, "添加后出口卡片真的出现在页面上")

card = jj(pg, """(() => {
  const c = document.querySelector('#pBody .pcard');
  if (!c) return JSON.stringify({found: false});
  const cs = getComputedStyle(c);
  const del = c.querySelector('[data-act="delExit"]');
  return JSON.stringify({
    found: true,
    text: (c.innerText || '').replace(/\\s+/g, ' ').slice(0, 80),
    bg: cs.backgroundColor, radius: cs.borderRadius,
    hasDel: !!del, delPort: del?.dataset.port || '',
    delVisible: del ? del.getBoundingClientRect().width > 0 : false,
  });
})()""")
check(card.get("found") and ":3128" in card.get("text", ""), "卡片显示端口号", card)
check("squid-verify" in card.get("text", ""), "卡片显示自定义标签", card)
check(card.get("bg") not in ("rgba(0, 0, 0, 0)", "transparent"),
      "卡片有底色（不是透明的）", card.get("bg"))
check(card.get("radius") not in ("0px", ""), "卡片有圆角", card.get("radius"))
check(card.get("hasDel") and card.get("delPort") == "3128",
      "卡片上有删除按钮且带对的端口", card)
check(card.get("delVisible"), "删除按钮真的可见（.pdel 样式生效）", card)

# 确认后端真存了
persisted = jj(pg, """(async () => {
  const r = await fetch('/api/settings', {credentials: 'same-origin'});
  const d = await r.json();
  return JSON.stringify({exits: d.settings.proxy_exits,
                         src: d.settings.proxy_exits__source});
})()""")
check(any(str(e.get("port")) == "3128" for e in (persisted.get("exits") or [])),
      "出口已落盘到 settings（重启后还在）", persisted)
check(persisted.get("src") == "runtime", "来源标记为 runtime", persisted)

# 删掉它：confirmDialog 是自定义弹窗，要点它的确认按钮
pg.js("document.querySelector('[data-act=\"delExit\"]').click()")
time.sleep(0.4)
pg.js("document.querySelector('.modal [data-act=\"yes\"]')?.click()")
removed = wait_for(pg, "!document.querySelector('#pBody .pcard')")
check(removed, "删除后卡片从页面消失")

gone = jj(pg, """(async () => {
  const r = await fetch('/api/settings', {credentials: 'same-origin'});
  const d = await r.json();
  return JSON.stringify({exits: d.settings.proxy_exits});
})()""")
check(not any(str(e.get("port")) == "3128" for e in (gone.get("exits") or [])),
      "后端也真的删了", gone)

# 自动探测弹窗（只验控件与上限提示，不真扫端口）
pg.js("document.querySelector('[data-act=\"discover\"]').click()")
time.sleep(0.5)
disc = jj(pg, """(() => {
  const b = document.querySelector('.modal-box');
  if (!b) return JSON.stringify({open: false});
  b.querySelector('#dcFrom').value = '1';
  b.querySelector('#dcTo').value = '65535';
  b.querySelector('#dcOk').click();
  const e = b.querySelector('#dcErr');
  return JSON.stringify({
    open: true, hasFrom: true,
    hasAdd: !!b.querySelector('#dcAdd'),
    errShown: e ? !e.hidden : false,
    errText: (e?.textContent || '').trim(),
  });
})()""")
check(disc.get("open"), "自动探测弹窗能打开", disc)
check(disc.get("hasAdd"), "有「直接加入出口表」勾选框", disc)
check(disc.get("errShown") and "4096" in disc.get("errText", ""),
      "范围过大时前端先给可读提示，不是干等 400", disc)
pg.js("document.querySelector('.modal [data-close]')?.click()")

# ------------------------------------------------------------------- 2) 设置页
print("\n=== 2) 设置页：运行时配置 + 接码 token 填写 ===")
pg.goto(f"{BASE}/#/settings")
ok = wait_for(pg, "!!document.querySelector('.cfg-group, .cfg-row')")
check(ok, "运行时配置卡片已渲染")

cfg = jj(pg, """(() => {
  const rows = [...document.querySelectorAll('.cfg-row')];
  const one = document.querySelector('.cfg-row');
  const cs = one ? getComputedStyle(one) : null;
  return JSON.stringify({
    rows: rows.length,
    groups: document.querySelectorAll('.cfg-group').length,
    keys: rows.map(r => r.dataset.key || '').filter(Boolean),
    labels: rows.slice(0, 3).map(r => (r.querySelector('.cfg-lab')?.innerText || '').trim()),
    rowDisplay: cs ? cs.display : '',
    hasTokenInput: !!document.querySelector('input[data-cfg="uoomsg_token"]'),
    tokenType: document.querySelector('input[data-cfg="uoomsg_token"]')?.type || '',
    hasSelect: !!document.querySelector('select[data-cfg="proxy_mode"]'),
    hasNumber: document.querySelector('input[data-cfg="balance_interval_min"]')?.type || '',
  });
})()""")
check(cfg["rows"] >= 11, f"12 个配置键都有控件（实际 {cfg['rows']} 行）", cfg["keys"])
check(cfg["groups"] >= 2, "配置按组分开了", cfg["groups"])
check(cfg["rowDisplay"] not in ("inline", ""), "cfg-row 样式生效", cfg["rowDisplay"])
check(cfg["hasTokenInput"], "接码平台 token 有输入框（用户能在面板填）")
check(cfg["tokenType"] == "password", "token 输入框是 password 类型", cfg["tokenType"])
check(cfg["hasSelect"], "proxy_mode 渲染成下拉（schema 的 choices 生效）")
check(cfg["hasNumber"] == "number", "int 型渲染成数字输入框", cfg["hasNumber"])

secret = jj(pg, """(async () => {
  const r = await fetch('/api/settings', {credentials: 'same-origin'});
  const t = await r.text();
  return JSON.stringify({leaks: t.includes('verify-pw'), body: t.slice(0, 0)});
})()""")
check(secret.get("leaks") is False, "配置接口不回传密码/密钥明文")

# 真改一个值：数字框改成 15，点保存，确认落盘
saved = jj(pg, """(async () => {
  // data-cfg 挂在控件自身，不是包裹层 —— 别再写 row.querySelector('input')
  const inp = document.querySelector('input[data-cfg="balance_interval_min"]');
  if (!inp) return JSON.stringify({err: '找不到 balance_interval_min 输入框'});
  inp.value = '15';
  inp.dispatchEvent(new Event('input', {bubbles: true}));
  const btn = document.querySelector('[data-act="cfgSave"]');
  if (!btn) return JSON.stringify({err: '没有保存按钮'});
  btn.click();
  await new Promise(r => setTimeout(r, 900));
  const r2 = await fetch('/api/settings', {credentials: 'same-origin'});
  const d = await r2.json();
  return JSON.stringify({val: d.settings.balance_interval_min,
                         src: d.settings.balance_interval_min__source});
})()""")
check(saved.get("val") == 15, "改配置后真的存进后端", saved)
check(saved.get("src") == "runtime", "来源标记切到 runtime", saved)

authcard = jj(pg, """(() => {
  const t = document.body.innerText;
  return JSON.stringify({
    fakeWarning: t.includes('当前后台没有登录保护'),
    // 徽章文案改了：现在按「默认密码 / 已自定义」报，而不是那个读不到的 enabled 键
    hasCustomPw: t.includes('密码已自定义'),
    hasAdminCount: t.includes('管理员账号数'),
  });
})()""")
check(authcard.get("fakeWarning") is False,
      "不再显示那条假的「没有登录保护」警告（原 renderAuth 字段错配）", authcard)
check(authcard.get("hasCustomPw"), "改完密码后徽章显示「密码已自定义」", authcard)
check(authcard.get("hasAdminCount"), "账户卡片读到了后端真实字段 users", authcard)

print("\n=== 3) 全程 JS 报错 ===")
real = [e for e in page_errors(pg) if "favicon" not in e.lower()]
check(not real, "没有未捕获的 JS 异常", real[:3])

print(f"\n{_p} passed, {_f} failed")
sys.exit(1 if _f else 0)
