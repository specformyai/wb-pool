"""
自动注册模块
============
流程：
  1. 从 uoomsg 取一个实体卡号码
  2. 调用 register.Registrar.start() 发腾讯短信验证码
  3. 轮询 uoomsg 等验证码
  4. 调用 register.Registrar.finish() 完成登录入池
  5. 释放 uoomsg 号码（成功）或拉黑（失败）

线程安全：每个自动注册任务独立运行，结果写入 _tasks 字典，
WebUI 通过 /api/auto_register/status/<task_id> 轮询。
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from . import uoomsg as uum
from .register import Registrar

# 任务状态保留时间（秒）
TASK_TTL = 3600


class AutoRegTask:
    def __init__(self, task_id: str, invite_code: str = "", label: str = ""):
        self.id = task_id
        self.invite_code = invite_code
        self.label = label
        self.created_at = time.time()
        self.status = "pending"          # pending | running | done | failed | stopped
        self.steps: list[str] = []
        self.result: dict[str, Any] = {}
        self.stop_flag = False           # 外部停止标志

    def log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.steps.append(f"[{ts}] {msg}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "steps": self.steps,
            "result": self.result,
            "age": round(time.time() - self.created_at, 1),
        }


class AutoRegistrar:
    def __init__(self, registrar: Registrar, uoomsg_token: str):
        self.registrar = registrar
        self.token = uoomsg_token
        self._tasks: dict[str, AutoRegTask] = {}
        self._lock = threading.Lock()

    def _gc(self) -> None:
        with self._lock:
            stale = [tid for tid, t in self._tasks.items()
                     if time.time() - t.created_at > TASK_TTL]
            for tid in stale:
                del self._tasks[tid]

    def start(self, invite_code: str = "", label: str = "", count: int = 1) -> dict[str, Any]:
        """启动 count 个异步自动注册任务，立即返回 task_id 列表。"""
        self._gc()
        task_ids = []
        for i in range(max(1, min(count, 20))):  # 限制 1-20 个
            task = AutoRegTask(secrets.token_urlsafe(8), invite_code=invite_code,
                             label=f"{label}_batch_{i+1}" if count > 1 and label else label)
            with self._lock:
                self._tasks[task.id] = task
            t = threading.Thread(target=self._run, args=(task,), daemon=True)
            t.start()
            task_ids.append(task.id)
        return {"ok": True, "task_ids": task_ids, "count": len(task_ids)}

    def stop(self, task_id: str) -> dict[str, Any]:
        """设置停止标志，任务会在下一个检查点终止。"""
        with self._lock:
            task = self._tasks.get(task_id)
        if not task:
            return {"ok": False, "error": "任务不存在"}
        if task.status in ("done", "failed", "stopped"):
            return {"ok": False, "error": f"任务已完成（{task.status}）"}
        task.stop_flag = True
        task.log("收到停止信号")
        return {"ok": True}

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            t = self._tasks.get(task_id)
        return t.to_dict() if t else None

    def list_tasks(self) -> list[dict[str, Any]]:
        self._gc()
        with self._lock:
            return [t.to_dict() for t in self._tasks.values()]

    def _run(self, task: AutoRegTask) -> None:
        task.status = "running"
        phone = None
        try:
            # 检查点：取号前
            if task.stop_flag:
                task.log("已停止（取号前）")
                task.status = "stopped"
                return

            # 1. 取号
            task.log("uoomsg 取号中（实卡过滤）…")
            res = uum.get_phone(self.token)
            if not res["ok"]:
                task.log(f"取号失败: {res['error']}")
                task.status = "failed"
                task.result = {"error": res["error"]}
                return
            phone = res["phone"]
            if res.get("skipped_virtual"):
                task.log(f"跳过虚拟号: {res['skipped_virtual']}")
            task.log(f"取到号码: {phone}")

            # 检查点：发码前
            if task.stop_flag:
                task.log("已停止（发码前），释放号码")
                uum.release(self.token, phone)
                task.status = "stopped"
                return

            # 2. 发腾讯验证码
            task.log("向腾讯发送短信验证码…")
            reg_res = self.registrar.start(phone)
            if not reg_res.get("ok"):
                err = reg_res.get("error", "发码失败")
                task.log(f"发码失败: {err}，拉黑该号")
                uum.block(self.token, phone)
                phone = None
                task.status = "failed"
                task.result = {"error": err, "log": reg_res.get("log", [])}
                return
            session_id = reg_res["session_id"]
            task.log(f"验证码已发出，出口: {reg_res.get('proxy', 'direct')}")

            # 检查点：等码前
            if task.stop_flag:
                task.log("已停止（等码前），释放号码")
                uum.release(self.token, phone)
                task.status = "stopped"
                return

            # 3. 等验证码
            task.log("轮询 uoomsg 等待验证码（最多 300s）…")
            sms_res = uum.get_sms(self.token, phone, timeout_s=300, poll_interval=5)
            if not sms_res["ok"]:
                task.log(f"等码超时: {sms_res['error']}，释放号码")
                uum.release(self.token, phone)
                phone = None
                task.status = "failed"
                task.result = {"error": sms_res["error"]}
                return
            code = sms_res["code"]
            task.log(f"收到验证码: {code}（原文: {sms_res['raw'][:60]}）")

            # 检查点：提交前
            if task.stop_flag:
                task.log("已停止（提交前），释放号码")
                uum.release(self.token, phone)
                task.status = "stopped"
                return

            # 4. 提交验证码入池
            task.log("提交验证码，登录并入池…")
            fin = self.registrar.finish(session_id, code,
                                        label=task.label or "auto",
                                        invite_code=task.invite_code)
            if not fin.get("ok"):
                err = fin.get("error", "登录失败")
                task.log(f"登录失败: {err}，拉黑号码")
                uum.block(self.token, phone)
                phone = None
                task.status = "failed"
                task.result = {"error": err, "log": fin.get("log", [])}
                return

            # 5. 释放号码（登录成功后号已不需要）
            task.log(f"登录成功，释放号码 {phone}")
            uum.release(self.token, phone)
            phone = None

            inv = fin.get("invite")
            if inv:
                task.log("邀请码：" + ("已绑定" if inv.get("ok") else inv.get("error", "未绑定")))

            task.log(f"入池完成: {fin['masked']}，积分 {fin.get('credits')}")
            task.status = "done"
            task.result = fin

        except Exception as exc:  # noqa: BLE001
            task.log(f"意外异常: {exc}")
            task.status = "failed"
            task.result = {"error": str(exc)}
            if phone:
                try:
                    uum.release(self.token, phone)
                except Exception:  # noqa: BLE001
                    pass
