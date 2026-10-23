"""任务执行者（批次 15c）：从 TaskQueue 领取任务、调用处理器、提交结果。规格见 docs/spec-26-task-queue.md。

同一套代码既可以在 Web 进程里以线程运行（开发与小规模部署），也可以在独立的 worker 进程里运行（apps/worker）。
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import Any

from core.app.ports import ConcurrencyError, Task, TaskQueue, TransientError

class TaskFailed(Exception):
    """处理器已自行处理并记录了失败（例如已把错误写进文档记录）：任务置 FAILED（不重试），错误文字即异常消息原文。"""


Beat = Callable[..., None]                       # beat(progress: str | None = None)：续租并（可选）报告进度
Handler = Callable[[Task, Beat], None]


class Worker:
    """run_once：按 handlers 的种类领取一个任务并执行，返回是否领到了任务。
    - 处理器正常返回 → complete；
    - 抛 TransientError → fail(retryable=True)（未超过最大次数时回到队列）；
    - 抛 TaskFailed → fail(retryable=False)，错误文字为异常消息原文；
    - 抛其他异常 → fail(retryable=False)，错误文字为 redact(异常)；
    - 提交时发现租约已不属于自己（ConcurrencyError，例如执行太久被别人重新领取）→ 放弃本次结果，不再提交。
    处理器通过 beat(progress) 续租：租约到期时刻 = 当前时钟 + lease_seconds（按任务种类取 lease_seconds，
    给的是 int 时所有种类相同）。beat 在租约已失时抛 ConcurrencyError，处理器不应吞掉它。
    执行完（无论成败）调用 on_finished(任务的最新状态)，其中的异常被忽略。
    start(threads) 启动若干线程，各线程的 worker_id 为 f"{worker_id}-{序号}"；领不到任务时等待 poll_seconds，
    notify() 立即唤醒。close() 停止领取新任务，并等待正在执行的任务结束后返回。"""

    def __init__(self, tasks: TaskQueue, handlers: Mapping[str, Handler], *, worker_id: str,
                 lease_seconds: int | Mapping[str, int] = 1800, poll_seconds: float = 1.0,
                 redact: Callable[[BaseException], str] = lambda exc: f"{type(exc).__name__}: {exc}",
                 on_finished: Callable[[Task], Any] | None = None) -> None:
        if not handlers:
            raise ValueError("至少需要一个任务种类的处理器")
        self._tasks, self._handlers = tasks, dict(handlers)
        self._worker_id, self._lease, self._poll = worker_id, lease_seconds, poll_seconds
        self._redact, self._on_finished = redact, on_finished
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def kinds(self) -> list[str]:
        return list(self._handlers)

    def _lease_for(self, kind: str) -> int:
        return self._lease if isinstance(self._lease, int) else self._lease.get(kind, 1800)

    def run_once(self, worker_id: str | None = None) -> bool:
        worker_id = worker_id or self._worker_id
        task = None
        for kind in self._handlers:
            task = self._tasks.claim([kind], worker_id=worker_id, lease_seconds=self._lease_for(kind))
            if task is not None:
                break
        if task is None:
            return False
        lease = self._lease_for(task.kind)

        def beat(progress: str | None = None) -> None:
            self._tasks.heartbeat(task.task_id, worker_id=worker_id, lease_seconds=lease, progress=progress)

        try:
            self._handlers[task.kind](task, beat)
        except ConcurrencyError:
            return True
        except TaskFailed as exc:
            self._finish(task, lambda: self._tasks.fail(task.task_id, worker_id=worker_id, error=str(exc),
                                                        retryable=False))
        except TransientError as exc:
            self._finish(task, lambda: self._tasks.fail(task.task_id, worker_id=worker_id, error=self._redact(exc),
                                                        retryable=True))
        except Exception as exc:
            self._finish(task, lambda: self._tasks.fail(task.task_id, worker_id=worker_id, error=self._redact(exc),
                                                        retryable=False))
        else:
            self._finish(task, lambda: self._tasks.complete(task.task_id, worker_id=worker_id))
        return True

    def _finish(self, task: Task, commit: Callable[[], Any]) -> None:
        try:
            commit()
        except ConcurrencyError:
            return
        if self._on_finished is not None:
            try:
                self._on_finished(self._tasks.get(task.task_id))
            except Exception:
                pass

    # —— 线程 ——

    def start(self, threads: int = 1) -> None:
        if threads < 1:
            raise ValueError("线程数必须为正数")
        if self._threads:
            return
        for number in range(1, threads + 1):
            thread = threading.Thread(target=self._loop, args=(f"{self._worker_id}-{number}",),
                                      name=f"haina-{self._worker_id}-{number}", daemon=True)
            self._threads.append(thread)
            thread.start()

    def notify(self) -> None:
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join()

    def _loop(self, worker_id: str) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once(worker_id)
            except Exception:
                worked = False          # 队列暂时不可用：等待后重试
            if not worked:
                self._wake.wait(self._poll)
                self._wake.clear()
