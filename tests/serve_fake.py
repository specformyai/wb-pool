#!/usr/bin/env python3
"""
起一个「接了假上游」的完整 wb-pool 实例，用来在浏览器里点真实 UI，
而不碰任何真手机号 / 真腾讯接口。

    .venv/bin/python tests/serve_fake.py 9190

之后浏览器开 http://127.0.0.1:9190/ ，管理密钥 fakekey。
验证码固定 123456，别人的邀请码用 friendcode99999。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("WB_API_KEY", "fakekey")
os.environ.setdefault("WB_PROXY_MODE", "off")

import tempfile

from tests.test_register_flow import reset_state, start_server

port = start_server()
base = f"http://127.0.0.1:{port}"
reset_state()

from app import invite, register, upstream  # noqa: E402

upstream.COPILOT = base
upstream.CONSOLE = base
register.COPILOT = base
register.CONSOLE = base
register.REDIRECT_URI = f"{base}/login/?platform=workbuddy"
invite.V2 = base + "/activity/workbuddy/invitation/v2"
invite.V1 = base + "/activity/workbuddy/invitation"

# 池子用临时文件，绝不动真 accounts.jsonl（必须在 import app.main 之前设好）
os.environ["WB_ACCOUNTS_FILE"] = tempfile.mkdtemp() + "/accounts.jsonl"

from app import main as app_main  # noqa: E402

assert str(app_main.ACCOUNTS_FILE) == os.environ["WB_ACCOUNTS_FILE"], \
    f"池文件没隔离开，指向了 {app_main.ACCOUNTS_FILE}"

print(f"假上游 {base}｜WebUI http://127.0.0.1:{sys.argv[1] if len(sys.argv) > 1 else 9190}"
      f"｜密钥 fakekey｜验证码 123456", flush=True)

import uvicorn  # noqa: E402

uvicorn.run(app_main.app, host="127.0.0.1",
            port=int(sys.argv[1]) if len(sys.argv) > 1 else 9190,
            log_level="warning")
