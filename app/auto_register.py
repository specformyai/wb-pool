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

号码去重（2026-09-11 加，此前完全没有）：
  uoomsg 在并发下会把**同一个号发给多个请求** —— 实测一批任务里
  两个任务拿到同一个 15xxxxxxxxx，4 个注册会话拿到同一个号。后果不只是
  浪费：几个任务抢同一条短信，其中一个失败还会 block 掉正在被别人使用的
  好号（连坐）。平台侧不保证独占，**必须由我们自己去重**。

  三层排除，都在取号时通过 uum.get_phone(exclude=...) 生效：
    ① _claimed     —— 本进程其它任务正在用的号（取号成功即占用，finally 释放）
    ② _recent_fail —— 近期失败过的号，冷却 RECENT_FAIL_TTL 秒内不再取
    ③ 池内已有账号 —— 含 disabled/dead，避免为已有号再花一次短信费
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
# 注册失败的号进冷却，避免立刻被重新取到再花一次短信费。
# 不用 uoomsg 的 block：那是永久拉黑，而多数失败（验证码过期、被并发任务
# 抢走短信、链路抖动）不是号码本身的问题 —— 见 _retire_phone。
RECENT_FAIL_TTL = 3600.0


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
        # 本任务占用的号码。局部变量 phone 在各分支会被置 None（表示"已归还，
        # 别再重复 release"），所以占用释放不能依赖它，必须独立记一份。
        self.claimed_phone: str | None = None

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
        # 正在被本进程任务占用的号码（归一化后的 11 位）
        self._claimed: set[str] = set()
        # 近期失败的号码 → 失败时刻，冷却期内不再取
        self._recent_fail: dict[str, float] = {}

    def _gc(self) -> None:
        with self._lock:
            stale = [tid for tid, t in self._tasks.items()
                     if time.time() - t.created_at > TASK_TTL]
            for tid in stale:
                del self._tasks[tid]

    # ---------------- 号码占用与排除 ----------------

    def _claim(self, phone: str) -> bool:
        """占用号码。已被别的任务占用则返回 False（调用方应退回重取）。"""
        key = uum._normalize(phone)
        if not key:
            return False
        with self._lock:
            if key in self._claimed:
                return False
            self._claimed.add(key)
        return True

    def _unclaim(self, phone: str | None) -> None:
        """释放占用。幂等 —— 多条退出路径都会调它。"""
        if not phone:
            return
        with self._lock:
            self._claimed.discard(uum._normalize(phone))

    def _mark_fail(self, phone: str | None) -> None:
        with self._lock:
            if phone:
                self._recent_fail[uum._normalize(phone)] = time.time()
            # 顺手清理过期条目，别让这个 dict 无限长
            cutoff = time.time() - RECENT_FAIL_TTL
            for k in [k for k, v in self._recent_fail.items() if v < cutoff]:
                del self._recent_fail[k]

    def _pool_phones(self) -> set[str]:
        """池内已有账号的号码（含 disabled/dead）。

        为已有号再注册一次会白花一次短信费，而且 pool.add() 会走 updated
        分支覆盖 token —— 不是我们想在批量注册里发生的事。
        """
        try:
            return {uum._normalize(a.phone) for a in self.registrar.pool.all()
                    if a.phone}
        except Exception:  # noqa: BLE001
            return set()

    def _exclude_set(self) -> set[str]:
        """取号排除集合 = 其它任务占用 ∪ 近期失败（未过冷却）∪ 池内已有。"""
        cutoff = time.time() - RECENT_FAIL_TTL
        with self._lock:
            claimed = set(self._claimed)
            recent = {k for k, v in self._recent_fail.items() if v >= cutoff}
        return claimed | recent | self._pool_phones()

    def _retire_phone(self, task: AutoRegTask, phone: str, reason: str) -> None:
        """注册失败后处置号码：默认释放 + 本地冷却，只在上游明确拒绝该号时拉黑。

        原实现对「登录失败/验证码校验失败」一律 uum.block()，等于永久拉黑。
        但这类失败的常见原因是验证码过期、短信被并发任务抢走、链路抖动 ——
        号码本身没问题。2026-09-11 就因为提交了从 Cloudflare 拦截页刮出来的
        假验证码，把一个好号拉黑了。
        """
        low = (reason or "").lower()
        # 上游明确表示这个号不能用（号段不支持、被运营商拒收、风控拒绝该号）
        number_rejected = any(k in reason for k in (
            "手机号", "号码不", "不支持该", "号段")) or "invalid phone" in low
        try:
            if number_rejected:
                uum.block(self.token, phone)
                task.log(f"号码被上游拒绝，已拉黑: {reason[:80]}")
            else:
                uum.release(self.token, phone)
                self._mark_fail(phone)
                task.log(f"已释放号码并加入 {int(RECENT_FAIL_TTL)}s 冷却"
                         f"（未拉黑：失败原因不指向号码本身）")
        except Exception as exc:  # noqa: BLE001
            task.log(f"处置号码失败: {exc}")

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
                # 进冷却。能走到这里的检查点都在发码之后，也就是这个号已经
                # 白花过一次短信且没收到码 —— 立刻允许重取只会再花一次钱。
                # 这条最初漏了，导致 150s 超时（最常见的失败路径）完全不进
                # 冷却表：定时器置 stop_flag 后，get_sms 一返回就被这个函数
                # 提前拦下，下面那个带 _mark_fail 的 sms-timeout 分支永远
                # 走不到（2026-09-11 并发实测：排除数恒等于池内号数）。
                self._mark_fail(phone)
                task.log("号码已释放并加入冷却")
            except Exception as exc:  # noqa: BLE001
                task.log(f"释放号码失败: {exc}")
        return True

    def _run(self, task: AutoRegTask) -> None:
        task.status = "running"
        phone = None
        session_id = None
        timeout_timer = threading.Timer(task.remaining(), self._expire, args=(task,))
        timeout_timer.daemon = True
        timeout_timer.start()
        try:
            # 检查点：取号前
            if self._finish_if_aborted(task, None, "取号前"):
                return

            # 1. 取号（排除：其它任务占用 / 近期失败 / 池内已有）
            exclude = self._exclude_set()
            task.log(f"uoomsg 取号中（实卡过滤，排除 {len(exclude)} 个已占用/已有号）…")
            res = uum.get_phone(self.token, exclude=exclude)
            if res.get("ok"):
                phone = res["phone"]
                if res.get("skipped_virtual"):
                    task.log(f"跳过虚拟号: {res['skipped_virtual']}")
                if res.get("skipped_duplicate"):
                    task.log(f"跳过重复号（已被占用或池内已有）: "
                             f"{len(res['skipped_duplicate'])} 个")
                if res.get("cf_blocked"):
                    task.log(f"取号期间 {res['cf_blocked']} 次 Cloudflare 拦截，已重试")
                # 占用：平台可能在两次调用之间把同一个号发给别的任务，
                # exclude 是取号那一刻的快照，claim 才是真正的互斥。
                if not self._claim(phone):
                    task.log(f"号码 {phone} 已被其它任务占用，释放并重取")
                    try:
                        uum.release(self.token, phone)
                    except Exception:  # noqa: BLE001
                        pass
                    phone = None
                    res = uum.get_phone(self.token, exclude=self._exclude_set())
                    if res.get("ok"):
                        phone = res["phone"]
                        if not self._claim(phone):
                            try:
                                uum.release(self.token, phone)
                            except Exception:  # noqa: BLE001
                                pass
                            phone = None
                            res = {"ok": False,
                                   "error": "连续取到已被占用的号码，稍后重试"}
                if phone:
                    task.claimed_phone = phone
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
                task.log(f"发码失败: {err}")
                self._retire_phone(task, phone, err)
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
                # 加冷却：这个号刚发过一条短信没收到，立刻重取只会再花一次钱
                self._mark_fail(phone)
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
                task.log(f"登录失败: {err}")
                self._retire_phone(task, phone, err)
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
            # 任务没走到成功，派生的注册会话必须一起收掉：否则它会在 Registrar
            # 里挂满 10 分钟，同号守卫让这个号一直显示「有进行中的会话」。
            # 成功路径 finish() 已经把会话 pop 掉，cancel 幂等返回 found=False。
            if session_id and task.status != "done":
                cancel = getattr(self.registrar, "cancel", None)
                if cancel:
                    try:
                        cancel(session_id)
                    except Exception:  # noqa: BLE001
                        pass
            # 占用释放的唯一出口：超时 / 停止 / 异常 / 成功全部经过这里。
            # 用 task.claimed_phone 而不是局部 phone —— 后者在各分支被置 None。
            self._unclaim(task.claimed_phone)
            task.claimed_phone = None
