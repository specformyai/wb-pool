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

import re
import secrets
import threading
import time
from typing import Any

from . import uoomsg as uum
from .register import Registrar

# 任务状态保留时间（秒）
TASK_TTL = 3600
TASK_TIMEOUT = 150
TERMINAL_STATUSES = {"done", "failed", "stopped"}


def _strip_ts(line: str) -> str:
    """去掉日志行首的 [HH:MM:SS]，WebUI 的「当前步骤」只需要正文。"""
    return re.sub(r"^\[\d{2}:\d{2}:\d{2}\]\s*", "", line or "")


class AutoRegTask:
    def __init__(self, task_id: str, invite_code: str = "", label: str = ""):
        self.id = task_id
        self.invite_code = invite_code
        self.label = label
        self.created_at = time.time()
        self.finished_at = 0.0           # 必须在 status 之前：setter 会读它
        self.deadline = self.created_at + TASK_TIMEOUT
        self.status = "pending"          # pending | running | done | failed | stopped
        self.steps: list[str] = []
        self.result: dict[str, Any] = {}
        self.stop_flag = False           # 外部停止标志
        self.timeout_flag = False

    # status 包一层 property：终态时刻由 setter 统一记录。
    # `task.status = ...` 的赋值点散布在 _run / _expire / _finish_if_aborted 里共 10 处，
    # 逐个手写 finished_at 必漏，而 WebUI 的「耗时」列就靠它。
    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        self._status = value
        if value in TERMINAL_STATUSES and not self.finished_at:
            self.finished_at = time.time()

    def log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.steps.append(f"[{ts}] {msg}")

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.time())

    def to_dict(self) -> dict[str, Any]:
        """一个任务 = 一个号，所以 target 恒为 1，进度/成功/失败按这个口径展开。

        state / logs 是 status / steps 的别名：WebUI 读的是前者，
        旧版只给后者，于是徽章渲染成字面 "undefined"、进度恒 0/0、日志区恒空。
        旧键名一并保留，curl 脚本与文档不受影响。
        """
        terminal = self.status in TERMINAL_STATUSES
        return {
            "id": self.id,
            "status": self.status,
            "state": self.status,
            "steps": self.steps,
            "logs": self.steps,
            "result": self.result,
            "age": round(time.time() - self.created_at, 1),
            "timeout_s": TASK_TIMEOUT,
            "label": self.label,
            "target": 1,
            "done": 1 if terminal else 0,
            "ok": 1 if self.status == "done" else 0,
            "fail": 1 if self.status == "failed" else 0,
            "started_at": int(self.created_at),
            "finished_at": int(self.finished_at) or None,
            "current": "" if terminal else _strip_ts(self.steps[-1] if self.steps else ""),
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
        if task.status in TERMINAL_STATUSES:
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

    def clear_finished(self) -> dict[str, Any]:
        """只清理终态任务；运行中任务保留，避免清单消失但线程仍在跑。"""
        with self._lock:
            finished = [tid for tid, task in self._tasks.items()
                        if task.status in TERMINAL_STATUSES]
            running = sum(1 for task in self._tasks.values()
                          if task.status not in TERMINAL_STATUSES)
            for tid in finished:
                del self._tasks[tid]
            remaining = len(self._tasks)
        return {"ok": True, "cleared": len(finished),
                "running": running, "remaining": remaining}

    def _expire(self, task: AutoRegTask) -> None:
        """定时器兜底：阻塞中的外部请求也不能让任务列表永久显示运行中。"""
        with self._lock:
            if task.status in TERMINAL_STATUSES:
                return
            task.timeout_flag = True
            task.stop_flag = True
            task.status = "failed"
            task.result = {"error": f"任务超过 {TASK_TIMEOUT} 秒，已自动停止"}
            task.log(f"任务超过 {TASK_TIMEOUT} 秒，已自动停止")

    def _finish_if_aborted(self, task: AutoRegTask, phone: str | None,
                           stage: str) -> bool:
        if not task.stop_flag and task.remaining() > 0:
            return False
        timed_out = task.timeout_flag or task.remaining() <= 0
        if timed_out:
            task.timeout_flag = True
            task.status = "failed"
            task.result = {"error": f"任务超过 {TASK_TIMEOUT} 秒，已自动停止"}
            if not task.steps or "已自动停止" not in task.steps[-1]:
                task.log(f"任务超过 {TASK_TIMEOUT} 秒，已自动停止（{stage}）")
        else:
            task.status = "stopped"
            task.log(f"已停止（{stage}）")
        if phone:
            try:
                uum.release(self.token, phone)
                task.log("号码已释放")
            except Exception as exc:  # noqa: BLE001
                task.log(f"释放号码失败: {exc}")
        return True

    def _run(self, task: AutoRegTask) -> None:
        task.status = "running"
        phone = None
        timeout_timer = threading.Timer(task.remaining(), self._expire, args=(task,))
        timeout_timer.daemon = True
        timeout_timer.start()
        try:
            # 检查点：取号前
            if self._finish_if_aborted(task, None, "取号前"):
                return

            # 1. 取号
            task.log("uoomsg 取号中（实卡过滤）…")
            res = uum.get_phone(self.token)
            if res.get("ok"):
                phone = res["phone"]
                if res.get("skipped_virtual"):
                    task.log(f"跳过虚拟号: {res['skipped_virtual']}")
                task.log(f"取到号码: {phone}")

            # 检查点：取号返回后。即使迟到结果是失败，也不能覆盖超时终态。
            if self._finish_if_aborted(task, phone, "取号后"):
                phone = None
                return
            if not res["ok"]:
                task.log(f"取号失败: {res['error']}")
                task.status = "failed"
                task.result = {"error": res["error"]}
                return

            # 检查点：发码前
            if self._finish_if_aborted(task, phone, "发码前"):
                phone = None
                return

            # 2. 发腾讯验证码
            task.log("向腾讯发送短信验证码…")
            reg_res = self.registrar.start(phone, origin="auto")
            if self._finish_if_aborted(task, phone, "发码后"):
                phone = None
                return
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
            if self._finish_if_aborted(task, phone, "等码前"):
                phone = None
                return

            # 3. 等验证码
            sms_timeout = max(1, int(task.remaining()))
            task.log(f"轮询 uoomsg 等待验证码（任务总上限 {TASK_TIMEOUT}s，当前剩余 {sms_timeout}s）…")
            sms_res = uum.get_sms(self.token, phone, timeout_s=sms_timeout, poll_interval=5)
            if self._finish_if_aborted(task, phone, "等码后"):
                phone = None
                return
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
            if self._finish_if_aborted(task, phone, "提交前"):
                phone = None
                return

            # 4. 提交验证码入池
            task.log("提交验证码，登录并入池…")
            fin = self.registrar.finish(session_id, code,
                                        label=task.label or "auto",
                                        invite_code=task.invite_code)
            if self._finish_if_aborted(task, phone, "提交后"):
                phone = None
                return
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
            if task.timeout_flag:
                # 定时器已经把任务置为超时失败，迟到异常不能改写原因。
                if phone:
                    try:
                        uum.release(self.token, phone)
                    except Exception:  # noqa: BLE001
                        pass
            elif task.stop_flag:
                self._finish_if_aborted(task, phone, "异常后")
            else:
                task.status = "failed"
                task.result = {"error": str(exc)}
                if phone:
                    try:
                        uum.release(self.token, phone)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            timeout_timer.cancel()
