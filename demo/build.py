#!/usr/bin/env python3
"""把 web/ 打包成一个能在 GitHub Pages 上跑的纯静态演示站。

为什么需要构建这一步
--------------------
生产形态下后端把 web/ 挂在 /static，index.html 里写的是 /static/xxx 这种
**绝对路径**（25 处）。Pages 把站点放在 https://<user>.github.io/<repo>/ 这个
**子路径**下，绝对路径会解析到域名根，全部 404 —— 页面白屏且只有 network 面板
能看出原因。

所以构建做四件事：

  1) 绝对路径 /static/xxx -> 相对路径 ./xxx（子路径、根路径两种部署都能跑）
  2) 在 importmap **之前**注入 mock.js（必须早于任何模块加载，
     因为 index.html 尾部那段内联脚本首屏就打 /api/auth/state；
     晚一步就会真打网络、拿 404，然后 gotoLogin() 跳出演示站）
  3) 去掉 index.html 尾部那段「未登录就跳 /login」的兜底守卫 ——
     演示站没有 /login 这个路由，跳过去就是 404
  4) 落一个 .nojekyll，否则 Pages 的 Jekyll 会吞掉下划线开头的文件

产物是 dist/，直接就是 Pages 的发布目录。
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import sys
from typing import NoReturn

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DEMO = ROOT / "demo"
DIST = ROOT / "dist"

FIXTURES = DEMO / "fixtures.json"


def fail(msg: str) -> NoReturn:
    """标 NoReturn，类型检查器才知道 fail() 之后的代码不可达
    （否则 re.search 的 Optional 会被报成可能为 None）。"""
    print(f"  !! {msg}")
    sys.exit(1)


print("=== 1) 前置检查 ===")
for p in (WEB / "index.html", DEMO / "mock.js", FIXTURES):
    if not p.exists():
        fail(f"缺少 {p.relative_to(ROOT)}")
    print(f"  {p.relative_to(ROOT)}  {p.stat().st_size:>8d}B")

fx = json.loads(FIXTURES.read_text(encoding="utf-8"))
print(f"  fixtures 端点数: {len(fx)}")
if len(fx) < 10:
    fail("fixtures 端点太少，演示站会大面积空白")

print("\n=== 2) 复制静态资源 ===")
if DIST.exists():
    shutil.rmtree(DIST)
DIST.mkdir()

copied = 0
for src in sorted(WEB.iterdir()):
    if src.is_file() and src.name != "login.html":
        # login.html 不带进演示站：没有后端就没有登录这件事
        shutil.copy2(src, DIST / src.name)
        copied += 1
print(f"  web/ -> dist/  {copied} 个文件（login.html 已排除）")

shutil.copy2(DEMO / "mock.js", DIST / "mock.js")
shutil.copy2(FIXTURES, DIST / "fixtures.json")
(DIST / ".nojekyll").write_text("", encoding="utf-8")
print("  + mock.js, fixtures.json, .nojekyll")

print("\n=== 3) 改写 index.html ===")
html = (WEB / "index.html").read_text(encoding="utf-8")
orig = html

# --- 3a) 绝对路径 -> 相对路径 ---
n_abs = html.count('"/static/')
html = html.replace('"/static/', '"./')
print(f"  /static/ 绝对路径 -> ./  共 {n_abs} 处")
if n_abs == 0:
    fail("一处 /static/ 都没匹配到，index.html 结构可能变了")

# --- 3b) 注入 mock.js，必须在 importmap 之前 ---
anchor = '<script type="importmap">'
if anchor not in html:
    fail("找不到 importmap 锚点")
inject = (
    "<!-- 静态演示站：拦截 fetch 返回预抓的 fixture。必须在任何模块加载前执行。 -->\n"
    '<script src="./mock.js"></script>\n'
)
html = html.replace(anchor, inject + anchor, 1)
print("  已在 importmap 前注入 mock.js")

# --- 3c) 拆掉「未登录跳 /login」的兜底守卫 ---
# 那段是 (async () => { ... })(); 包着的 IIFE，里面会 gotoLogin()。
# 演示站没有 /login，跳过去 404。直接换成无条件启动。
m = re.search(
    r"  \(async \(\) => \{.*?\}\)\(\);", html, re.S)
if not m:
    fail("找不到首屏登录守卫那段 IIFE")
replacement = (
    "  // 演示站没有后端也没有 /login 路由，去掉登录守卫，直接启动。\n"
    "  refreshIcons();\n"
    "  start();"
)
html = html[:m.start()] + replacement + html[m.end():]
print("  已移除首屏登录守卫（演示站无 /login 路由）")

if "gotoLogin" in html:
    # gotoLogin 的定义还在但已无人调用，留着无害；确认没有调用点即可
    calls = len(re.findall(r"gotoLogin\(\)", html))
    print(f"  gotoLogin 调用点剩余: {calls}（应为 0）")
    if calls:
        fail("仍有 gotoLogin 调用，演示站会跳出去")

# --- 3d) 标题与 meta 标注这是演示站 ---
html = html.replace(
    "<title>wb-pool 账号池网关</title>",
    "<title>wb-pool 账号池网关 · 在线演示</title>", 1)
html = html.replace(
    '<meta name="robots" content="noindex" />',
    '<meta name="robots" content="index" />', 1)
print("  标题标注「在线演示」，robots 放开索引")

if html == orig:
    fail("index.html 一个字节都没变，改写逻辑失效")

(DIST / "index.html").write_text(html, encoding="utf-8")
print(f"  dist/index.html  {len(html)}B")

print("\n=== 4) 校验产物 ===")
out = (DIST / "index.html").read_text(encoding="utf-8")
checks = [
    ("无 /static/ 绝对路径", '"/static/' not in out),
    ("mock.js 在 importmap 之前",
     out.index("mock.js") < out.index('<script type="importmap">')),
    ("无 gotoLogin 调用", "gotoLogin()" not in out),
    ("保留 importmap", '"@/shared.js"' in out),
    ("保留 start() 调用", "start()" in out),
    ("fixtures.json 已就位", (DIST / "fixtures.json").exists()),
    ("mock.js 已就位", (DIST / "mock.js").exists()),
    (".nojekyll 已就位", (DIST / ".nojekyll").exists()),
]
bad = 0
for label, ok in checks:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        bad += 1

n_files = len(list(DIST.iterdir()))
size = sum(f.stat().st_size for f in DIST.iterdir() if f.is_file())
print(f"\n  dist/ 共 {n_files} 项，{size // 1024}KB")

print(f"\n=== {'构建成功' if bad == 0 else f'构建有 {bad} 项不通过'} ===")
sys.exit(1 if bad else 0)
