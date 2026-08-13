"""
邀请模块
========
实测结论（2026-08-13，账号 181****4225）：

  活动有两套并行、**各自独立的邀请码**：
    v1  /activity/workbuddy/invitation/*       字段 camelCase，码 3r0j3lytxt3hjz
    v2  /activity/workbuddy/invitation/v2/*    字段 snake_case，码 fdzf6ib6akbgexd
  两个码都稳定（各调 3 次不变），但 **v1 活动已结束**：
  拿一个不存在的码打 v1/bind 返回 `12312 The activity has ended`，
  打 v2/bind 返回 `12314 invite code is not for current activity`。
  → 所以对外只用 v2 的码。

  端点：
    GET  v2/my-code         {invite_code, expires_at}
    GET  v2/my-progress     {invite_code, invite_count, invited_users, total_credits,
                             valid_invite_count, base_credits, promotion_credits,
                             payment_credits, cap_reached, cap_value:30000}
    GET  v2/my-rewards      {total_credits, ..., details:[]}
    GET  v2/invite-records  {friends, total_invited, total_used, total_credits}
    POST v2/bind  {"inviteCode": "..."}   ← 注意请求体是 **camelCase**，
                  snake_case 会 400 'BindInviteCodeRequest.InviteCode required'

  错误码：
    12313 cannot use your own invite code   ← 自己的码绑自己，服务端直接拒
    12314 invite code is not for current activity
    12312 The activity has ended
"""
from __future__ import annotations

from typing import Any

import httpx

from . import upstream

V2 = "https://copilot.tencent.com/activity/workbuddy/invitation/v2"
V1 = "https://copilot.tencent.com/activity/workbuddy/invitation"

BIND_ERRORS = {
    12313: "不能用自己的邀请码（换池里另一个账号的码）",
    12314: "这个邀请码不属于当前活动",
    12312: "活动已结束",
    12311: "只有活动期内注册的新用户才能绑邀请码（该号注册时活动已截止）",
}


def _get(path: str, token: str, proxy: str | None = None,
         base: str | None = None) -> dict[str, Any]:
    # 注意：base 必须在调用时解析。写成 `base: str = V2` 会在 import 时把值绑死，
    # 之后改 invite.V2（测试重定向、或换域名）都不生效。
    base = base if base is not None else V2
    with httpx.Client(proxy=proxy, timeout=25, follow_redirects=True) as c:
        r = c.get(base + path, headers=upstream.auth_headers(token))
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}", "body": r.text[:200]}
    j = r.json()
    if j.get("code") not in (0, None):
        return {"error": f"code={j.get('code')} {j.get('msg')}"}
    return j.get("data") or {}


def my_code(token: str, proxy: str | None = None) -> str:
    """取本账号的邀请码（v2，即当前有效的那套）。"""
    d = _get("/my-code", token, proxy)
    return d.get("invite_code") or ""


def overview(token: str, proxy: str | None = None) -> dict[str, Any]:
    """邀请总览：码 + 进度 + 奖励 + 好友记录。"""
    prog = _get("/my-progress", token, proxy)
    rew = _get("/my-rewards", token, proxy)
    rec = _get("/invite-records", token, proxy)
    return {
        "invite_code": prog.get("invite_code") or _get("/my-code", token, proxy).get("invite_code", ""),
        "invite_link": f"https://www.codebuddy.cn/events/invite?id={prog.get('invite_code', '')}"
                       if prog.get("invite_code") else "",
        "invite_count": prog.get("invite_count", 0),
        "valid_invite_count": prog.get("valid_invite_count", 0),
        "invited_users": prog.get("invited_users") or [],
        "total_credits": prog.get("total_credits", 0),
        "base_credits": prog.get("base_credits", 0),
        "promotion_credits": prog.get("promotion_credits", 0),
        "payment_credits": prog.get("payment_credits", 0),
        "cap_value": prog.get("cap_value", 30000),
        "cap_reached": prog.get("cap_reached", False),
        "rewards": rew,
        "records": rec,
        "v1_code": _get("/my-code", token, proxy, base=V1).get("inviteCode", ""),  # V1 见上方注释
        "v1_note": "v1 活动已结束，该码仅供参考，实际请用上面的 invite_code",
    }


def bind(token: str, invite_code: str, proxy: str | None = None) -> dict[str, Any]:
    """
    给**当前这个账号**绑定别人的邀请码（即"我是被邀请的一方"）。
    只能在账号还没绑过时生效；用自己的码会被服务端拒（12313）。
    请求体必须是 camelCase 的 inviteCode。
    """
    code = (invite_code or "").strip()
    if not code:
        return {"ok": False, "error": "邀请码不能为空"}
    with httpx.Client(proxy=proxy, timeout=25) as c:
        r = c.post(V2 + "/bind",
                   headers=upstream.auth_headers(token),
                   json={"inviteCode": code})
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": f"HTTP {r.status_code} {r.text[:150]}"}
    ec = j.get("code")
    if ec in (0, None):
        return {"ok": True, "msg": j.get("msg") or "绑定成功", "data": j.get("data")}
    return {"ok": False, "code": ec,
            "error": BIND_ERRORS.get(ec, f"code={ec} {j.get('msg')}")}
