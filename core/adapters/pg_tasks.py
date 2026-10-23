"""TaskQueue 的 PostgreSQL 实现（psycopg 3）。规格见 docs/spec-26-task-queue.md 与 core/app/ports.py。

- 表结构：core/adapters/pg_migrations/003_tasks.sql。
- 所有时间由构造时传入的 clock 给出，作为 SQL 参数传入，不使用数据库的 now()（契约测试要能控制时间）。
- claim 用 SELECT ... FOR UPDATE SKIP LOCKED，多个进程并发领取不会拿到同一个任务。
- 每个方法一个短事务；实例内用锁串行化对连接的使用（psycopg 连接不能被多个线程同时使用）。
"""
from __future__ import annotations

import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime, timedelta
from threading import RLock
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from core.adapters.pg_registry import _connect
from core.app.ports import MAX_TASK_ATTEMPTS, ConcurrencyError, PermanentError, Task, TaskStatus

_COLUMNS = ("task_id, idempotency_key, kind, payload, attempt, status, worker_id, last_error, progress, "
            "enqueued_at, finished_at")


def _task(row: tuple[Any, ...]) -> Task:
    return Task(row[0], row[1], row[2], row[3], row[4], TaskStatus(row[5]),
                row[6], row[7], row[8], row[9], row[10])


class PgTaskRepo:
    def __init__(self, dsn: str, *, schema: str = "public", clock: Callable[[], datetime]) -> None:
        self._dsn, self._schema, self._clock = dsn, schema, clock
        self._lock = RLock()
        self._connection = _connect(dsn, schema)
        try:
            with self._connection.transaction():
                self._connection.execute("SELECT 1 FROM tasks LIMIT 0")
        except psycopg.errors.UndefinedTable as exc:
            self._connection.close()
            raise ValueError("数据库尚未迁移到任务队列结构，请先运行迁移") from exc

    @property
    def _conn(self) -> psycopg.Connection:
        if self._connection.closed or self._connection.broken:
            self._connection = _connect(self._dsn, self._schema)
        return self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def enqueue(self, idempotency_key: str, kind: str, payload: Mapping[str, Any]) -> Task:
        with self._lock, self._conn.transaction():
            row = self._conn.execute(f"SELECT {_COLUMNS} FROM tasks WHERE idempotency_key=%s",
                                     (idempotency_key,)).fetchone()
            if row is None:
                row = self._conn.execute(
                    f"INSERT INTO tasks(task_id,idempotency_key,kind,payload,attempt,status,enqueued_at) "
                    f"VALUES (%s,%s,%s,%s,1,'queued',%s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING {_COLUMNS}",
                    (uuid.uuid4().hex, idempotency_key, kind, Jsonb(dict(payload)), self._clock())).fetchone()
                if row is None:
                    row = self._conn.execute(f"SELECT {_COLUMNS} FROM tasks WHERE idempotency_key=%s",
                                             (idempotency_key,)).fetchone()
            return _task(row)

    def get(self, task_id: str) -> Task:
        with self._lock, self._conn.transaction():
            row = self._conn.execute(f"SELECT {_COLUMNS} FROM tasks WHERE task_id=%s", (task_id,)).fetchone()
            if row is None:
                raise PermanentError("任务不存在")
            return _task(row)

    def claim(self, kinds: Sequence[str], *, worker_id: str, lease_seconds: int) -> Task | None:
        if lease_seconds <= 0:
            raise ValueError("租约秒数必须为正数")
        if not kinds:
            return None
        with self._lock, self._conn.transaction():
            now = self._clock()
            while True:
                row = self._conn.execute(
                    f"SELECT {_COLUMNS} FROM tasks WHERE kind=ANY(%s) "
                    "AND (status='queued' OR (status='running' AND lease_until<=%s)) "
                    "ORDER BY seq LIMIT 1 FOR UPDATE SKIP LOCKED", (list(kinds), now)).fetchone()
                if row is None:
                    return None
                attempt = row[4]
                if row[5] == TaskStatus.RUNNING.value:
                    if attempt >= MAX_TASK_ATTEMPTS:
                        self._conn.execute(
                            "UPDATE tasks SET status='failed',last_error='lease expired',worker_id=NULL,"
                            "lease_until=NULL,finished_at=%s WHERE task_id=%s", (now, row[0]))
                        continue
                    attempt += 1
                claimed = self._conn.execute(
                    f"UPDATE tasks SET status='running',worker_id=%s,lease_until=%s,attempt=%s,"
                    f"progress=NULL,finished_at=NULL WHERE task_id=%s RETURNING {_COLUMNS}",
                    (worker_id, now + timedelta(seconds=lease_seconds), attempt, row[0])).fetchone()
                return _task(claimed)

    def _owned(self, task_id: str, worker_id: str) -> tuple[Any, ...]:
        row = self._conn.execute(f"SELECT {_COLUMNS},lease_until FROM tasks WHERE task_id=%s FOR UPDATE",
                                 (task_id,)).fetchone()
        if row is None:
            raise PermanentError("任务不存在")
        if row[5] != TaskStatus.RUNNING.value or row[6] != worker_id or row[11] <= self._clock():
            raise ConcurrencyError("任务租约已失效或不属于当前执行者")
        return row

    def complete(self, task_id: str, *, worker_id: str) -> None:
        with self._lock, self._conn.transaction():
            self._owned(task_id, worker_id)
            self._conn.execute("UPDATE tasks SET status='done',worker_id=NULL,lease_until=NULL,finished_at=%s "
                               "WHERE task_id=%s", (self._clock(), task_id))

    def fail(self, task_id: str, *, worker_id: str, error: str, retryable: bool) -> Task:
        with self._lock, self._conn.transaction():
            row = self._owned(task_id, worker_id)
            retry = retryable and row[4] < MAX_TASK_ATTEMPTS
            updated = self._conn.execute(
                f"UPDATE tasks SET status=%s,attempt=%s,last_error=%s,worker_id=NULL,lease_until=NULL,"
                f"finished_at=%s WHERE task_id=%s RETURNING {_COLUMNS}",
                (TaskStatus.QUEUED.value if retry else TaskStatus.FAILED.value,
                 row[4] + 1 if retry else row[4], error, None if retry else self._clock(), task_id)).fetchone()
            return _task(updated)

    def heartbeat(self, task_id: str, *, worker_id: str, lease_seconds: int, progress: str | None = None) -> None:
        if lease_seconds <= 0:
            raise ValueError("租约秒数必须为正数")
        with self._lock, self._conn.transaction():
            self._owned(task_id, worker_id)
            self._conn.execute("UPDATE tasks SET lease_until=%s,progress=COALESCE(%s,progress) WHERE task_id=%s",
                               (self._clock() + timedelta(seconds=lease_seconds), progress, task_id))

    def tasks(self, kind: str, *, statuses: Collection[TaskStatus] | None = None, limit: int = 1000) -> list[Task]:
        if limit < 1:
            raise ValueError("查询数量必须为正数")
        with self._lock, self._conn.transaction():
            if statuses is None:
                rows = self._conn.execute(f"SELECT {_COLUMNS} FROM tasks WHERE kind=%s ORDER BY seq LIMIT %s",
                                          (kind, limit)).fetchall()
            else:
                rows = self._conn.execute(f"SELECT {_COLUMNS} FROM tasks WHERE kind=%s AND status=ANY(%s) "
                                          "ORDER BY seq LIMIT %s",
                                          (kind, [status.value for status in statuses], limit)).fetchall()
            return [_task(row) for row in rows]

    def count(self, kind: str, status: TaskStatus) -> int:
        with self._lock, self._conn.transaction():
            return self._conn.execute("SELECT count(*) FROM tasks WHERE kind=%s AND status=%s",
                                      (kind, status.value)).fetchone()[0]

    def find(self, idempotency_key: str) -> Task | None:
        with self._lock, self._conn.transaction():
            row = self._conn.execute(f"SELECT {_COLUMNS} FROM tasks WHERE idempotency_key=%s",
                                     (idempotency_key,)).fetchone()
            return _task(row) if row is not None else None

    def latest_per(self, kind: str, field: str) -> list[Task]:
        with self._lock, self._conn.transaction():
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM (SELECT seq,{_COLUMNS},"
                "row_number() OVER (PARTITION BY payload->%s ORDER BY seq DESC) AS position "
                "FROM tasks WHERE kind=%s AND payload ? %s) AS latest WHERE position=1 ORDER BY seq",
                (field, kind, field)).fetchall()
            return [_task(row) for row in rows]
