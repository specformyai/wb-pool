#!/usr/bin/env python3
"""自动注册超时与任务清理的无网络回归测试。"""
from __future__ import annotations

import time
import unittest
from typing import Any, cast
from unittest.mock import patch

import os
import sys

# 让 `python tests/xxx.py` 裸跑就能 import app.*，不依赖 PYTHONPATH。
# 与 tests/ 下其他测试的既有写法保持一致。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auto_register as ar


class FakeRegistrar:
    # 签名必须跟真 Registrar.start 对齐：_run() 调的是
    # start(phone, origin="auto")（会话来源标记，用于把自动任务的会话
    # 隔离出手动注册页）。少一个 origin 参数会抛 TypeError，被 _run 的
    # except 吞掉 → 任务在「发码」阶段就失败，get_sms 压根不会被调用，
    # 于是等码相关的断言全部落空。
    def start(self, phone: str, origin: str = "manual") -> dict:
        return {"ok": True, "session_id": "session", "proxy": "direct"}

    def finish(self, session_id: str, code: str, label: str = "", invite_code: str = "") -> dict:
        return {"ok": True, "masked": "138****8000", "credits": 100}


class AutoRegisterControlsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registrar = ar.AutoRegistrar(cast(Any, FakeRegistrar()), "test-token")

    def test_timeout_constant_is_150_seconds(self) -> None:
        self.assertEqual(ar.TASK_TIMEOUT, 150)
        task = ar.AutoRegTask("deadline")
        self.assertAlmostEqual(task.deadline - task.created_at, 150, places=3)
        self.assertEqual(task.to_dict()["timeout_s"], 150)

    def test_clear_finished_keeps_running_tasks(self) -> None:
        statuses = ["pending", "running", "done", "failed", "stopped"]
        for status in statuses:
            task = ar.AutoRegTask(status)
            task.status = status
            self.registrar._tasks[task.id] = task

        result = self.registrar.clear_finished()
        self.assertEqual(result, {"ok": True, "cleared": 3, "running": 2, "remaining": 2})
        self.assertEqual(set(self.registrar._tasks), {"pending", "running"})

    def test_blocked_task_is_marked_failed_by_timer(self) -> None:
        released: list[str] = []

        def slow_phone(_token: str) -> dict:
            time.sleep(0.16)
            return {"ok": True, "phone": "13800138000"}

        original_timeout = ar.TASK_TIMEOUT
        ar.TASK_TIMEOUT = 0.06
        try:
            with patch.object(ar.uum, "get_phone", side_effect=slow_phone), \
                 patch.object(ar.uum, "release", side_effect=lambda _token, phone: released.append(phone)):
                started = self.registrar.start()
                task_id = started["task_ids"][0]
                time.sleep(0.09)
                timed_out = self.registrar.get(task_id)
                self.assertIsNotNone(timed_out)
                assert timed_out is not None
                self.assertEqual(timed_out["status"], "failed")
                self.assertIn("自动停止", timed_out["result"]["error"])
                time.sleep(0.12)
                final = self.registrar.get(task_id)
                self.assertIsNotNone(final)
                assert final is not None
                self.assertEqual(final["status"], "failed", "迟到结果不能把超时任务改回成功")
                self.assertEqual(released, ["13800138000"])
        finally:
            ar.TASK_TIMEOUT = original_timeout

    def test_late_phone_failure_cannot_overwrite_timeout(self) -> None:
        def slow_failed_phone(_token: str) -> dict:
            time.sleep(0.16)
            return {"ok": False, "error": "迟到的取号错误"}

        original_timeout = ar.TASK_TIMEOUT
        ar.TASK_TIMEOUT = 0.06
        try:
            with patch.object(ar.uum, "get_phone", side_effect=slow_failed_phone):
                started = self.registrar.start()
                task_id = started["task_ids"][0]
                time.sleep(0.22)
                result = self.registrar.get(task_id)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result["status"], "failed")
                self.assertIn("超过", result["result"]["error"])
                self.assertNotIn("迟到的取号错误", result["result"]["error"])
        finally:
            ar.TASK_TIMEOUT = original_timeout

    def test_sms_wait_uses_remaining_deadline(self) -> None:
        sms_timeouts: list[int] = []

        def get_sms(_token: str, _phone: str, timeout_s: int, poll_interval: int) -> dict:
            sms_timeouts.append(timeout_s)
            return {"ok": False, "error": "test stop"}

        with patch.object(ar.uum, "get_phone", return_value={"ok": True, "phone": "13800138000"}), \
             patch.object(ar.uum, "get_sms", side_effect=get_sms), \
             patch.object(ar.uum, "release", return_value="ok"):
            started = self.registrar.start()
            task_id = started["task_ids"][0]
            task = None
            for _ in range(100):
                task = self.registrar.get(task_id)
                if task and task["status"] in ar.TERMINAL_STATUSES:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task["status"], "failed")
            self.assertEqual(len(sms_timeouts), 1)
            self.assertGreaterEqual(sms_timeouts[0], 1)
            self.assertLessEqual(sms_timeouts[0], 150)


if __name__ == "__main__":
    unittest.main(verbosity=2)
