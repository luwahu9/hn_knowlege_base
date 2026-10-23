"""检索索引的用户组标签同步作业（批次 16a）。规格见 docs/spec-28-auth.md 第三节。"""
from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from apps.web.knowledge import china_now
from apps.web.services import Redactor
from core.adapters.memory import InMemoryTaskRepo
from core.app.permissions import PermissionService
from core.app.ports import Indexer, Task, TaskQueue, TaskStatus
from core.app.worker import Beat, TaskFailed, Worker

ACL_SYNC = "acl_sync"
BATCH = 200


class AclSyncJobs:
    """种类 "acl_sync"，payload {"revision"}（入队时的授权修订号，仅作记录），幂等键 f"acl_sync:{revision}:{随机}"。

    - enqueue(revision)：已有排队中的同步任务 → 不再入队，返回 None（排队的任务执行时读取最新状态，已能覆盖）；
      否则入队并返回任务 ID。
    - handle：反复取 service.stale(BATCH)，按标签分组调用 indexer.set_group_tags(版本们, 标签)，成功后 record_indexed；
      直到没有过期标签。某组调用失败时逐个版本重试，单个版本仍失败的记下并在本次任务中不再处理；
      结束时有失败的版本 → TaskFailed("以下版本的检索标签未能更新：…"，最多列 20 个)。每批之后 beat(f"已同步 {n}")。
    - run_workers 为 False 时只入队。"""

    KIND = ACL_SYNC

    def __init__(self, service: PermissionService, indexer: Indexer, redactor: Redactor, *,
                 tasks: TaskQueue | None = None, run_workers: bool = True, worker_id: str = "web",
                 poll_seconds: float = 1.0) -> None:
        self._service, self._indexer, self._redactor = service, indexer, redactor
        self._tasks = tasks if tasks is not None else InMemoryTaskRepo(china_now)
        self._run_workers = run_workers
        self.worker = Worker(self._tasks, {self.KIND: self.handle}, worker_id=f"{worker_id}-acl", lease_seconds=1800,
                             poll_seconds=poll_seconds, redact=redactor.error)

    def start(self) -> None:
        if self._run_workers:
            self.worker.start()

    def close(self) -> None:
        self.worker.close()

    def enqueue(self, revision: int) -> str | None:
        if self._tasks.tasks(self.KIND, statuses={TaskStatus.QUEUED}, limit=1):
            return None
        task = self._tasks.enqueue(f"{self.KIND}:{revision}:{uuid.uuid4().hex}", self.KIND, {"revision": revision})
        self.worker.notify()
        return task.task_id

    def handle(self, task: Task, beat: Beat) -> None:
        failed: list[str] = []
        done = 0
        while True:
            pending = [(version, tags) for version, tags in self._service.stale(BATCH + len(failed))
                       if version not in failed][:BATCH]
            if not pending:
                break
            groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
            for version, tags in pending:
                groups[tags].append(version)
            for tags, versions in groups.items():
                try:
                    self._indexer.set_group_tags(versions, list(tags))
                    self._service.repo.record_indexed({version: tags for version in versions})
                    done += len(versions)
                except Exception:
                    for version in versions:
                        try:
                            self._indexer.set_group_tags([version], list(tags))
                            self._service.repo.record_indexed({version: tags})
                            done += 1
                        except Exception:
                            failed.append(version)
            beat(f"已同步 {done}")
        if failed:
            raise TaskFailed("以下版本的检索标签未能更新：" + "、".join(sorted(failed)[:20]))

    def status(self) -> dict[str, Any]:
        """{"stale": 过期标签的版本数（最多数到 10000）, "queued", "running", "failed"}。"""
        return {"stale": len(self._service.stale(10_000)),
                **{status.value: self._tasks.count(self.KIND, status)
                   for status in (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.FAILED)}}
