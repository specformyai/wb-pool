#!/usr/bin/env python3
"""
uoomsg CF 哨兵 + 号码去重 的离线回归（假 httpx，不打真实平台、不烧接码余额）。

覆盖 2026-09-11 定位的两个真实故障：
  ① 取号「秒失败」    —— CF 拦截 HTML 被 fullmatch(\\d{11}) 判成「号码格式异常」并立刻 return
  ② 验证码校验失败    —— CF 拦截 HTML 通过了 get_sms 的否定式守卫，提码正则从页面里
                          刮出 6 位数字当验证码提交给腾讯

判据是**双向**的：HTML 必须被拦（正对照），真实 API 文本必须照常通过（负对照）。
只测一半会把好响应也吞掉，那比原 bug 更糟。

跑法（线上 venv 没装 pytest，直接执行）：
    .venv/bin/python tests/test_uoomsg_cfguard.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("WB_DATA_DIR", "/tmp/wb-cfguard-test-data")

from app import uoomsg as uum  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(f"{name}  {detail}".rstrip())


# --------------------------------------------------------------------------- #
# 真实响应样本（全部取自 2026-09-11 在 165 上的实测输出）
# --------------------------------------------------------------------------- #
CF_HTML = """<!DOCTYPE html>
<!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->
<head><title>Attention Required! | Cloudflare</title></head>
<body>
  <div class="cf-error-details">
    Error 1010 Ray ID: 395301abcdef Please enable cookies.
    Cloudflare Ray ID: 8f2a11 &bull; Your IP: 1.2.3.4
  </div>
</body></html>"""

# 平台正常响应
OK_BALANCE = "3.80"
OK_PHONE = "15171738623"
OK_PHONE_MVNO = "16778468226"          # 165/167/170/171/162 号段，应被虚拟号过滤
OK_WAITING = "尚未收到包含关键字“腾讯科技”的短信，请5秒后再收取。请确保设置了正确的关键字。[尚未收到]"
OK_SMS = "【腾讯科技】450944为您的登录验证码，请于5分钟内填写，如非本人操作，请忽略本短信。"
OK_SMS_OTHER = "【某某平台】998877 是您的验证码。"   # 非腾讯短信，不该提码
OK_ERROR = "ERROR: token 无效"


class FakeResp:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status_code = status


class FakeClient:
    """按 code 参数依次吐预设响应，并记录收到的 headers。"""

    def __init__(self, script: dict[str, list[str]], seen: dict) -> None:
        self._script = script
        self._seen = seen

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        code = (params or {}).get("code", "")
        self._seen.setdefault("codes", []).append(code)
        self._seen.setdefault("phones", []).append((params or {}).get("phone"))
        queue = self._script.get(code) or ["<<no-script>>"]
        return FakeResp(queue.pop(0) if len(queue) > 1 else queue[0])


def install_fake(script: dict[str, list[str]]) -> dict:
    seen: dict = {}

    def factory(*a, **kw):
        seen["headers"] = kw.get("headers") or {}
        return FakeClient(script, seen)

    uum.httpx = type("M", (), {"Client": staticmethod(factory)})()
    return seen


REAL_HTTPX = uum.httpx


# =========================================================================== #
# 1. _looks_like_html 双向对照
# =========================================================================== #
check("html-detect: CF 页面", uum._looks_like_html(CF_HTML) is True)
check("html-detect: 余额文本放行", uum._looks_like_html(OK_BALANCE) is False)
check("html-detect: 号码放行", uum._looks_like_html(OK_PHONE) is False)
check("html-detect: 等码提示放行", uum._looks_like_html(OK_WAITING) is False)
check("html-detect: 真短信放行", uum._looks_like_html(OK_SMS) is False)
check("html-detect: ERROR 放行", uum._looks_like_html(OK_ERROR) is False)
check("html-detect: 空串放行", uum._looks_like_html("") is False)
# tab 分隔的 queryUsed 记录（含中文正文）不能误判
check("html-detect: queryUsed 记录放行",
      uum._looks_like_html(f"{OK_PHONE}\t0.45\t{OK_SMS}") is False)


# =========================================================================== #
# 2. _call 抛 UoomsgBlocked + 带浏览器 UA
# =========================================================================== #
seen = install_fake({"leftAmount": [CF_HTML]})
try:
    uum._call("tok", {"code": "leftAmount"})
    check("_call: HTML 抛异常", False, "没抛")
except uum.UoomsgBlocked as exc:
    check("_call: HTML 抛 UoomsgBlocked", True)
    check("_call: 异常信息含字节数", str(len(CF_HTML)) in str(exc), str(exc)[:60])
except Exception as exc:  # noqa: BLE001
    check("_call: HTML 抛 UoomsgBlocked", False, f"抛了 {type(exc).__name__}")

ua = (seen.get("headers") or {}).get("User-Agent", "")
check("_call: 带浏览器 UA", "Mozilla/5.0" in ua, ua[:40])
check("_call: 不是 httpx 默认 UA", "python-httpx" not in ua)

seen = install_fake({"leftAmount": [OK_BALANCE]})
check("_call: 正常文本原样返回", uum._call("tok", {"code": "leftAmount"}) == OK_BALANCE)


# =========================================================================== #
# 3. balance
# =========================================================================== #
install_fake({"leftAmount": [OK_BALANCE]})
check("balance: 正常解析", uum.balance("tok") == 3.80)

install_fake({"leftAmount": [CF_HTML]})
try:
    uum.balance("tok")
    check("balance: CF 时抛异常（不返回假数字）", False, "没抛")
except uum.UoomsgBlocked:
    check("balance: CF 时抛异常（不返回假数字）", True)


# =========================================================================== #
# 4. get_phone —— 症状①：CF 拦截不再秒失败，而是重试
# =========================================================================== #
install_fake({"getPhone": [CF_HTML, CF_HTML, OK_PHONE], "release": [""]})
res = uum.get_phone("tok", max_attempts=5)
check("get_phone: CF 后重试成功", res.get("ok") is True, str(res)[:100])
check("get_phone: 返回正确号码", res.get("phone") == OK_PHONE)
check("get_phone: 上报 CF 次数", res.get("cf_blocked") == 2, f"cf_blocked={res.get('cf_blocked')}")

# 全程 CF → 失败，但错误信息必须指向 Cloudflare 而不是「号码格式异常」
install_fake({"getPhone": [CF_HTML], "release": [""]})
res = uum.get_phone("tok", max_attempts=2)
check("get_phone: 全 CF 时失败", res.get("ok") is False)
check("get_phone: 错误指向 CF 而非号码格式",
      "Cloudflare" in res.get("error", "") and "格式异常" not in res.get("error", ""),
      res.get("error", "")[:80])

# 虚拟号仍被过滤（原有行为不能坏）
install_fake({"getPhone": [OK_PHONE_MVNO, OK_PHONE], "release": [""]})
res = uum.get_phone("tok", max_attempts=5)
check("get_phone: 虚拟号仍被跳过",
      res.get("ok") is True and OK_PHONE_MVNO in (res.get("skipped_virtual") or []))

# ERROR: 仍然立即返回（这是平台的确定性拒绝，不该重试）
install_fake({"getPhone": [OK_ERROR]})
res = uum.get_phone("tok", max_attempts=5)
check("get_phone: ERROR 立即返回", res.get("ok") is False and "ERROR" in res.get("error", ""))


# =========================================================================== #
# 5. get_phone exclude —— 号码去重
# =========================================================================== #
install_fake({"getPhone": [OK_PHONE, "13800138000"], "release": [""]})
res = uum.get_phone("tok", max_attempts=5, exclude={OK_PHONE})
check("get_phone: 排除集合生效", res.get("ok") is True and res.get("phone") == "13800138000",
      str(res)[:90])
check("get_phone: 上报重复号", OK_PHONE in (res.get("skipped_duplicate") or []))

# 排除集合格式归一化：+86 前缀 / 带空格 都应命中同一个号
install_fake({"getPhone": [OK_PHONE], "release": [""]})
res = uum.get_phone("tok", max_attempts=2, exclude={f"+86{OK_PHONE}"})
check("get_phone: +86 格式也能排除", res.get("ok") is False, str(res)[:90])

install_fake({"getPhone": [OK_PHONE], "release": [""]})
res = uum.get_phone("tok", max_attempts=2, exclude={f" {OK_PHONE} "})
check("get_phone: 带空格也能排除", res.get("ok") is False)

# 空 exclude 不影响正常取号
install_fake({"getPhone": [OK_PHONE]})
check("get_phone: exclude=None 正常", uum.get_phone("tok", exclude=None).get("ok") is True)
install_fake({"getPhone": [OK_PHONE]})
check("get_phone: exclude=空集 正常", uum.get_phone("tok", exclude=set()).get("ok") is True)


# =========================================================================== #
# 6. _code_from_sms —— 症状②：绝不从 CF 页面提码
# =========================================================================== #
# 这是最关键的一条：旧实现在这里刮出了 395301
check("code: 真短信提码", uum._code_from_sms(OK_SMS) == "450944")
check("code: CF 页面不提码", uum._code_from_sms(CF_HTML) is None,
      f"刮出了 {uum._code_from_sms(CF_HTML)}")
check("code: 等码提示不提码", uum._code_from_sms(OK_WAITING) is None)
check("code: 非腾讯短信不提码", uum._code_from_sms(OK_SMS_OTHER) is None)
check("code: ERROR 不提码", uum._code_from_sms(OK_ERROR) is None)
check("code: 空串不提码", uum._code_from_sms("") is None)
# extract_code 保持原语义（裸提码，会命中 HTML）—— 证明危险确实存在于旧路径
check("code: extract_code 仍是裸提码（对照）", uum.extract_code(CF_HTML) is not None)


# =========================================================================== #
# 7. get_sms 端到端
# =========================================================================== #
install_fake({"getMsg": [OK_SMS], "leftAmount": [OK_BALANCE]})
res = uum.get_sms("tok", OK_PHONE, timeout_s=5, poll_interval=1)
check("get_sms: 收到真验证码", res.get("ok") is True and res.get("code") == "450944", str(res)[:90])

# CF 期间不能返回假码；应超时失败并如实报告 CF
install_fake({"getMsg": [CF_HTML], "leftAmount": [OK_BALANCE]})
t0 = time.time()
res = uum.get_sms("tok", OK_PHONE, timeout_s=3, poll_interval=1)
check("get_sms: CF 不产生假验证码", res.get("ok") is False, str(res)[:90])
check("get_sms: 报告 CF 拦截", (res.get("cf_blocked") or 0) > 0, f"cf={res.get('cf_blocked')}")
check("get_sms: 超时时长合理", time.time() - t0 < 8)

# 先 CF 后真码 → 必须最终成功（瞬时故障可恢复）
install_fake({"getMsg": [CF_HTML, OK_SMS], "leftAmount": [OK_BALANCE]})
res = uum.get_sms("tok", OK_PHONE, timeout_s=6, poll_interval=1)
check("get_sms: CF 后恢复取到码", res.get("ok") is True and res.get("code") == "450944",
      str(res)[:90])

# 余额接口 CF 也不能中断等码
install_fake({"getMsg": [OK_SMS], "leftAmount": [CF_HTML]})
res = uum.get_sms("tok", OK_PHONE, timeout_s=5, poll_interval=1)
check("get_sms: 余额 CF 不阻断取码", res.get("ok") is True, str(res)[:90])

uum.httpx = REAL_HTTPX


# =========================================================================== #
# 汇总
# =========================================================================== #
print(f"\n=== uoomsg CF 哨兵 + 去重 回归 ===")
for p in PASS:
    print(f"  PASS  {p}")
for f in FAIL:
    print(f"  FAIL  {f}")
print(f"\n{len(PASS)} passed, {len(FAIL)} failed  (total {len(PASS) + len(FAIL)})")
sys.exit(1 if FAIL else 0)
