#!/usr/bin/env python3
"""起一个隔离的 wb-pool 实例供浏览器验证用。

隔离要点：
  * WB_DATA_DIR 指向临时目录 —— 不碰仓库 data/，也不碰生产
  * WB_BALANCE_INTERVAL_MIN=9999 关掉余额刷新（否则真打上游）
  * 不配 uoomsg token，注册链路自然走不通，不会烧接码余额
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = pathlib.Path(tempfile.mkdtemp(prefix="wbverify-"))

os.environ["WB_DATA_DIR"] = str(DATA)
os.environ["WB_ADMIN_KEY"] = "verify-admin-key"
os.environ["WB_BALANCE_INTERVAL_MIN"] = "9999"
os.environ["WB_CHECKIN_CRON"] = "0 4 * * *"
sys.path.insert(0, str(ROOT))

print(f"[verify] DATA_DIR = {DATA}", flush=True)

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("WB_VERIFY_PORT", "8931")), log_level="warning")
