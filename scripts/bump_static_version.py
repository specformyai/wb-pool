#!/usr/bin/env python3
"""按文件内容 sha1 重写 HTML 入口里的 importmap 与 <link> 版本号。

为什么需要这个脚本
------------------
web/*.html 用 importmap + ?v=<sha1> 做 cache-busting。改完 web/*.js 或
web/*.css 却不 bump，浏览器会合法地继续用缓存：ES module 有独立于 HTTP
缓存的模块图缓存，后端给的 Cache-Control: no-store 管得住 HTML，管不住
?v= 没变的模块 URL。

实测后果（2026-08-24）：shared.js / pages.js / pool.js 连续几轮改完都没
bump，用户一次都没看到，还反馈「刷新和强刷都没用」。

用法
----
    python3 scripts/bump_static_version.py                 # 处理 web/ 下全部 .html
    python3 scripts/bump_static_version.py --web path/to/web
    python3 scripts/bump_static_version.py --check         # 只报告，不写入

输出形如：
    login.html: 已更新 2 个条目
      loginpage.js         00000000 -> 3f2a91cc
      shared.css           4a8191e8 -> 4a8191e8

设计要点
--------
* 默认扫描 web/ 下所有 .html（原版只认死路径的 index.html，多入口时会漏）。
* 只改哈希真变了的条目，并打印 old -> new 便于人工核对，不静默重写。
* importmap 里登记了但文件不存在时告警并跳过，不静默产生坏 URL。
* --check 退出码 1 表示「有文件改了但没 bump」，可挂进 CI 当闸门。
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import sys

# importmap 条目：  "@/pool.js":  "/static/pool.js?v=14f986c1",
JS_PAT = re.compile(r'("@/([^"]+\.js)":\s*"/static/\2\?v=)([0-9a-f]+)(")')
# <link href="/static/pool.css?v=ce81ac5f">
CSS_PAT = re.compile(r'(href="/static/([^"?]+\.css)\?v=)([0-9a-f]+)(")')


def sha8(path: pathlib.Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()[:8]


def bump_one(html_file: pathlib.Path, web: pathlib.Path,
             check: bool) -> tuple[int, list[str]]:
    """处理单个 HTML 入口。返回 (待更新条目数, 缺失文件名列表)。"""
    html = orig = html_file.read_text(encoding="utf-8")
    changed: list[tuple[str, str, str]] = []
    missing: list[str] = []

    def repl(m: re.Match[str]) -> str:
        fname = m.group(2)
        f = web / fname
        if not f.exists():
            missing.append(fname)
            return m.group(0)
        h = sha8(f)
        if h != m.group(3):
            changed.append((fname, m.group(3), h))
        return m.group(1) + h + m.group(4)

    html = JS_PAT.sub(repl, html)
    html = CSS_PAT.sub(repl, html)

    if not changed:
        print(f"  {html_file.name}: 哈希已是最新")
        return 0, missing

    if not check:
        html_file.write_text(html, encoding="utf-8")
    tag = "[check] 需要 bump" if check else "已更新"
    print(f"  {html_file.name}: {tag} {len(changed)} 个条目")
    for fname, old, new in changed:
        print(f"    {fname:20} {old} -> {new}")
    return len(changed), missing


def main() -> int:
    ap = argparse.ArgumentParser()
    default_web = pathlib.Path(__file__).resolve().parents[1] / "web"
    ap.add_argument("--web", default=str(default_web),
                    help="web 静态目录（含 .html 入口）")
    ap.add_argument("--check", action="store_true",
                    help="只报告需要 bump 的条目，不写入；有待更新则退出码 1")
    args = ap.parse_args()

    web = pathlib.Path(args.web)
    if not web.is_dir():
        print(f"!! 找不到目录 {web}", file=sys.stderr)
        return 2

    entries = sorted(web.glob("*.html"))
    if not entries:
        print(f"!! {web} 下没有 .html 入口", file=sys.stderr)
        return 2

    total = 0
    all_missing: list[str] = []
    for f in entries:
        n, missing = bump_one(f, web, args.check)
        total += n
        all_missing += missing

    for fname in sorted(set(all_missing)):
        print(f"  !! HTML 引用了 {fname} 但文件不存在，已跳过", file=sys.stderr)

    if total == 0:
        print("  全部入口哈希均为最新")
        return 0
    return 1 if args.check else 0


if __name__ == "__main__":
    sys.exit(main())
