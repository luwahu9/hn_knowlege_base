"""内存版适配器：契约测试的参考实现，也用于单元测试和本地演示。

规格就是 core/app/ports.py 中各端口的 docstring，以及 tests/contracts/ 下的契约测试。
所有类都必须线程安全（用一把锁保护内部状态即可）。
"""
from __future__ import annotations

import copy
import hashlib
import threading
from collections.abc import Collection, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

from core.app.reindex import BuiltDoc, Rebuild
from core.domain.conversation import Conversation, ConversationSummary

from core.app.ports import (
    MAX_TASK_ATTEMPTS, AclAuditEntry, ConcurrencyError, PermanentError, Task, TaskStatus, VersionRecord,
)
from core.domain.access_policy import AclSnapshot, Folder
from core.domain.version_fsm import Transition, VersionState


class InMemoryBlobStore:
    """BlobStore 的内存实现。signed_url 返回 "memory://<key>?ttl=<ttl_seconds>"。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._blobs: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        with self._lock:
            content = bytes(data)
            digest = hashlib.sha256(content).hexdigest()
            if key in self._blobs:
                if self._blobs[key] != content:
                    raise PermanentError("键已存在，不能覆盖不同内容")
                return digest
            self._blobs[key] = content
            return digest

    def get(self, key: str) -> bytes:
        with self._lock:
            if key not in self._blobs:
                raise PermanentError("对象不存在")
            return self._blobs[key]

    def delete_prefix(self, prefix: str) -> int:
        from core.adapters.fs_blob import validate_key
        if not prefix.endswith("/"):
            raise PermanentError("前缀必须以 / 结尾")
        validate_key(prefix[:-1])
        with self._lock:
            keys = [key for key in self._blobs if key.startswith(prefix)]
            for key in keys:
                del self._blobs[key]
            return len(keys)

    def exists(self, key: str) -> bool:
        with self._lock:
            return key in self._blobs

    def signed_url(self, key: str, *, ttl_seconds: int) -> str:
        with self._lock:
            if not 0 < ttl_seconds <= 3600:
                raise PermanentError("链接有效期必须在 1 到 3600 秒之间")
            if key not in self._blobs:
                raise PermanentError("对象不存在")
            return f"memory://{key}?ttl={ttl_seconds}"


class InMemoryTaskRepo:
    """TaskQueue（含 TaskRepo）的内存实现。task_id 依次为 "t1"、"t2"……（批次 15c 扩展为 TaskQueue）"""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._tasks: dict[str, Task] = {}
        self._task_ids: dict[str, str] = {}
        self._leases: dict[str, datetime] = {}

    def enqueue(self, idempotency_key: str, kind: str, payload: Mapping[str, Any]) -> Task:
        with self._lock:
            if idempotency_key in self._task_ids:
                return self._copy_task(self._tasks[self._task_ids[idempotency_key]])
            task_id = f"t{len(self._tasks) + 1}"
            task = Task(task_id, idempotency_key, kind, copy.deepcopy(dict(payload)), 1, TaskStatus.QUEUED,
                        enqueued_at=self._clock())
            result = self._copy_task(task)
            tasks = self._tasks.copy()
            task_ids = self._task_ids.copy()
            tasks[task_id] = task
            task_ids[idempotency_key] = task_id
            self._tasks, self._task_ids = tasks, task_ids
            return result

    def get(self, task_id: str) -> Task:
        with self._lock:
            return self._copy_task(self._get_task(task_id))

    def claim(self, kinds: Sequence[str], *, worker_id: str, lease_seconds: int) -> Task | None:
        with self._lock:
            if lease_seconds <= 0:
                raise ValueError("租约时长必须大于零")
            if not kinds:
                return None
            now = self._clock()
            tasks = self._tasks.copy()
            leases = self._leases.copy()
            result = None
            # 字典保持入队顺序；先暂存整次领取的修改，复制失败时也不会部分提交。
            for task_id, task in self._tasks.items():
                if task.kind not in kinds:
                    continue
                if task.status is TaskStatus.RUNNING:
                    if self._leases[task_id] > now:
                        continue
                    if task.attempt >= MAX_TASK_ATTEMPTS:
                        tasks[task_id] = replace(
                            task, status=TaskStatus.FAILED, worker_id=None, last_error="lease expired",
                            finished_at=now,
                        )
                        leases.pop(task_id)
                        continue
                    task = replace(task, attempt=task.attempt + 1)
                elif task.status is not TaskStatus.QUEUED:
                    continue
                claimed = replace(task, status=TaskStatus.RUNNING, worker_id=worker_id, progress=None)
                leases[task_id] = now + timedelta(seconds=lease_seconds)
                result = self._copy_task(claimed)
                tasks[task_id] = claimed
                break
            self._tasks, self._leases = tasks, leases
            return result

    def complete(self, task_id: str, *, worker_id: str) -> None:
        with self._lock:
            task = self._owned_task(task_id, worker_id)
            tasks = self._tasks.copy()
            leases = self._leases.copy()
            tasks[task_id] = replace(task, status=TaskStatus.DONE, worker_id=None, finished_at=self._clock())
            leases.pop(task_id)
            self._tasks, self._leases = tasks, leases

    def fail(self, task_id: str, *, worker_id: str, error: str, retryable: bool) -> Task:
        with self._lock:
            task = self._owned_task(task_id, worker_id)
            retry = retryable and task.attempt < MAX_TASK_ATTEMPTS
            updated = replace(
                task,
                status=TaskStatus.QUEUED if retry else TaskStatus.FAILED,
                attempt=task.attempt + 1 if retry else task.attempt,
                worker_id=None,
                last_error=error,
                finished_at=None if retry else self._clock(),
            )
            result = self._copy_task(updated)
            tasks = self._tasks.copy()
            leases = self._leases.copy()
            tasks[task_id] = updated
            leases.pop(task_id)
            self._tasks, self._leases = tasks, leases
            return result

    def heartbeat(self, task_id: str, *, worker_id: str, lease_seconds: int, progress: str | None = None) -> None:
        with self._lock:
            if lease_seconds <= 0:
                raise ValueError("租约时长必须大于零")
            task = self._owned_task(task_id, worker_id)
            tasks, leases = self._tasks.copy(), self._leases.copy()
            if progress is not None:
                tasks[task_id] = replace(task, progress=progress)
            leases[task_id] = self._clock() + timedelta(seconds=lease_seconds)
            self._tasks, self._leases = tasks, leases

    def tasks(self, kind: str, *, statuses: Collection[TaskStatus] | None = None, limit: int = 1000) -> list[Task]:
        if limit < 1:
            raise ValueError("limit 必须为正数")
        with self._lock:
            selected = [task for task in self._tasks.values()
                        if task.kind == kind and (statuses is None or task.status in statuses)]
            return [self._copy_task(task) for task in selected[:limit]]

    def count(self, kind: str, status: TaskStatus) -> int:
        with self._lock:
            return sum(task.kind == kind and task.status is status for task in self._tasks.values())

    def find(self, idempotency_key: str) -> Task | None:
        with self._lock:
            task_id = self._task_ids.get(idempotency_key)
            return None if task_id is None else self._copy_task(self._tasks[task_id])

    def latest_per(self, kind: str, field: str) -> list[Task]:
        with self._lock:
            latest: dict[Any, Task] = {}
            for task in self._tasks.values():
                if task.kind == kind and field in task.payload:
                    value = task.payload[field]
                    latest.pop(value, None)
                    latest[value] = task
            return [self._copy_task(task) for task in latest.values()]

    @staticmethod
    def _copy_task(task: Task) -> Task:
        return replace(task, payload=copy.deepcopy(dict(task.payload)))

    def _get_task(self, task_id: str) -> Task:
        if task_id not in self._tasks:
            raise PermanentError("任务不存在")
        return self._tasks[task_id]

    def _owned_task(self, task_id: str, worker_id: str) -> Task:
        # 调用方已持有实例锁，检查和随后的更新位于同一临界区。
        task = self._get_task(task_id)
        if task.status is not TaskStatus.RUNNING or task.worker_id != worker_id:
            raise ConcurrencyError("任务未运行或租约不属于当前执行者")
        if self._leases[task_id] <= self._clock():
            raise ConcurrencyError("任务租约已到期")
        return task


class InMemoryVersionRepo:
    """VersionRepo 的内存实现。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._versions: dict[str, tuple[VersionRecord, tuple[Transition, ...]]] = {}

    def create(self, version_id: str, source_id: str, *, initial_state: VersionState, created_at: datetime) -> VersionRecord:
        with self._lock:
            if version_id in self._versions:
                raise PermanentError("版本已存在")
            record = VersionRecord(version_id, source_id, initial_state, None, created_at)
            self._versions[version_id] = (record, ())
            return record

    def get(self, version_id: str) -> VersionRecord:
        with self._lock:
            return self._entry(version_id)[0]

    def history(self, version_id: str) -> list[Transition]:
        with self._lock:
            return list(self._entry(version_id)[1])

    def apply(self, version_id: str, expected_state: VersionState, transition: Transition) -> VersionRecord:
        with self._lock:
            record, history = self._entry(version_id)
            if transition.from_state != expected_state:
                raise ValueError("转移的起始状态与期望状态不符")
            if record.state != expected_state:
                raise ConcurrencyError("版本当前状态与期望状态不符")
            updated = replace(record, state=transition.to_state)
            # 状态与审计组成同一条目，构造成功后才替换。
            self._versions[version_id] = (updated, history + (transition,))
            return updated

    def _entry(self, version_id: str) -> tuple[VersionRecord, tuple[Transition, ...]]:
        if version_id not in self._versions:
            raise PermanentError("版本不存在")
        return self._versions[version_id]


class InMemoryAclRepo:
    """AclRepo 的内存实现。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._folders: dict[str, Folder] = {}
        self._folder_acl: dict[str, frozenset[str]] = {}
        self._doc_override: dict[str, frozenset[str]] = {}
        self._history: list[AclAuditEntry] = []

    def add_folder(self, folder_id: str, parent_id: str | None, *, actor: str) -> None:
        with self._lock:
            if not actor.strip():
                raise ValueError("操作人不能为空")
            if folder_id in self._folders:
                raise PermanentError("文件夹已存在")
            if parent_id is not None and parent_id not in self._folders:
                raise PermanentError("父文件夹不存在")
            folders = self._folders.copy()
            folders[folder_id] = Folder(folder_id, parent_id)
            history = self._history + [AclAuditEntry("folder_parent", folder_id, parent_id, actor, None)]
            self._folders, self._history = folders, history

    def snapshot(self) -> AclSnapshot:
        with self._lock:
            return AclSnapshot(
                MappingProxyType(self._folders.copy()),
                MappingProxyType(self._folder_acl.copy()),
                MappingProxyType(self._doc_override.copy()),
            )

    def set_folder_acl(self, folder_id: str, group_ids: frozenset[str] | None, *, actor: str) -> None:
        with self._lock:
            if not actor.strip():
                raise ValueError("操作人不能为空")
            if folder_id not in self._folders:
                raise PermanentError("文件夹不存在")
            folder_acl = self._folder_acl.copy()
            if group_ids is None:
                folder_acl.pop(folder_id, None)
            else:
                folder_acl[folder_id] = group_ids
            history = self._history + [AclAuditEntry("folder_acl", folder_id, group_ids, actor, None)]
            self._folder_acl, self._history = folder_acl, history

    def set_doc_override(self, doc_id: str, group_ids: frozenset[str] | None, *, actor: str, reason: str) -> None:
        with self._lock:
            if not actor.strip():
                raise ValueError("操作人不能为空")
            if not reason.strip():
                raise ValueError("覆盖或取消覆盖必须填写原因")
            doc_override = self._doc_override.copy()
            if group_ids is None:
                doc_override.pop(doc_id, None)
            else:
                doc_override[doc_id] = group_ids
            history = self._history + [AclAuditEntry("doc_override", doc_id, group_ids, actor, reason)]
            self._doc_override, self._history = doc_override, history

    def history(self, target_id: str) -> list[AclAuditEntry]:
        with self._lock:
            return [entry for entry in self._history if entry.target_id == target_id]


class InMemoryScanIndex:
    """ScanIndex 的内存实现（批次 15d）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}

    def entries(self, root: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._entries.get(root, {}))

    def put(self, root: str, entries: Sequence[Any]) -> None:
        with self._lock:
            current = dict(self._entries.get(root, {}))
            current.update((entry.path, entry) for entry in entries)
            self._entries[root] = current

    def remove(self, root: str, paths: Collection[str]) -> None:
        with self._lock:
            current = dict(self._entries.get(root, {}))
            for path in paths:
                current.pop(path, None)
            self._entries[root] = current

    def save_run(self, report: dict[str, Any]) -> None:
        with self._lock:
            self._runs[report["scan_id"]] = copy.deepcopy(report)

    def runs(self, root: str, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit 必须为正数")
        with self._lock:
            rows = sorted((row for row in self._runs.values() if row["root"] == root), key=lambda row: row["scan_id"])
            rows.sort(key=lambda row: row["started_at"], reverse=True)       # 同一时刻按 scan_id 升序
            return copy.deepcopy(rows[:limit])

    def run(self, scan_id: str) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._runs.get(scan_id))


class InMemoryIdentityRepo:
    """IdentityRepo 的内存实现（批次 16a）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._users: dict[str, Any] = {}
        self._hashes: dict[str, str] = {}
        self._failures: dict[str, tuple[int, datetime | None]] = {}
        self._groups: dict[str, Any] = {}
        self._members: dict[str, set[str]] = {}
        self._sessions: dict[str, Any] = {}
        self._audit: list[Any] = []

    def create_user(self, user: Any, password_hash: str | None) -> None:
        from core.app.identity import AlreadyExists
        with self._lock:
            if user.user_id in self._users or any(u.username == user.username for u in self._users.values()):
                raise AlreadyExists("登录名已存在")
            self._users[user.user_id] = user
            if password_hash is not None:
                self._hashes[user.user_id] = password_hash

    def user(self, user_id: str) -> Any:
        with self._lock:
            return self._users.get(user_id)

    def user_by_name(self, username: str) -> Any:
        with self._lock:
            return next((u for u in self._users.values() if u.username == username), None)

    def users(self) -> list[Any]:
        with self._lock:
            return sorted(self._users.values(), key=lambda u: u.username)

    def save_user(self, user: Any) -> None:
        with self._lock:
            current = self._users.get(user.user_id)
            if current is None:
                raise KeyError(user.user_id)
            if current.username != user.username:
                raise ValueError("不能修改登录名")
            self._users[user.user_id] = user

    def password_hash(self, user_id: str) -> str | None:
        with self._lock:
            return self._hashes.get(user_id)

    def set_password_hash(self, user_id: str, password_hash: str) -> None:
        with self._lock:
            if user_id not in self._users:
                raise KeyError(user_id)
            self._hashes[user_id] = password_hash

    def failures(self, user_id: str) -> tuple[int, datetime | None]:
        with self._lock:
            return self._failures.get(user_id, (0, None))

    def record_failure(self, user_id: str, at: datetime) -> int:
        with self._lock:
            count = self._failures.get(user_id, (0, None))[0] + 1
            self._failures[user_id] = (count, at)
            return count

    def reset_failures(self, user_id: str) -> None:
        with self._lock:
            self._failures.pop(user_id, None)

    def create_group(self, group: Any) -> None:
        from core.app.identity import AlreadyExists
        with self._lock:
            if group.group_id in self._groups or any(g.name == group.name for g in self._groups.values()):
                raise AlreadyExists("组名已存在")
            self._groups[group.group_id] = group
            self._members[group.group_id] = set()

    def rename_group(self, group_id: str, name: str) -> None:
        from core.app.identity import AlreadyExists, Group
        with self._lock:
            if group_id not in self._groups:
                raise KeyError(group_id)
            if any(g.name == name and g.group_id != group_id for g in self._groups.values()):
                raise AlreadyExists("组名已存在")
            self._groups[group_id] = Group(group_id, name)

    def delete_group(self, group_id: str) -> None:
        with self._lock:
            if group_id not in self._groups:
                raise KeyError(group_id)
            del self._groups[group_id]
            del self._members[group_id]

    def groups(self) -> list[Any]:
        with self._lock:
            return sorted(self._groups.values(), key=lambda g: (g.name, g.group_id))

    def members(self, group_id: str) -> list[str]:
        with self._lock:
            if group_id not in self._members:
                raise KeyError(group_id)
            return sorted(self._members[group_id])

    def set_members(self, group_id: str, user_ids: Collection[str]) -> None:
        with self._lock:
            if group_id not in self._members:
                raise KeyError(group_id)
            missing = [user_id for user_id in user_ids if user_id not in self._users]
            if missing:
                raise KeyError(missing[0])
            self._members[group_id] = set(user_ids)

    def user_groups(self, user_id: str) -> frozenset[str]:
        with self._lock:
            return frozenset(group for group, members in self._members.items() if user_id in members)

    def create_session(self, session: Any) -> None:
        with self._lock:
            self._sessions[session.token_hash] = session

    def session(self, token_hash: str) -> Any:
        with self._lock:
            return self._sessions.get(token_hash)

    def touch_session(self, token_hash: str, at: datetime) -> None:
        with self._lock:
            if token_hash in self._sessions:
                self._sessions[token_hash] = replace(self._sessions[token_hash], last_seen=at)

    def delete_session(self, token_hash: str) -> None:
        with self._lock:
            self._sessions.pop(token_hash, None)

    def delete_user_sessions(self, user_id: str, *, keep: str | None = None) -> int:
        with self._lock:
            doomed = [key for key, s in self._sessions.items() if s.user_id == user_id and key != keep]
            for key in doomed:
                del self._sessions[key]
            return len(doomed)

    def append_audit(self, at: datetime, actor: str | None, action: str, target: str | None,
                     detail: dict[str, Any]) -> int:
        from core.app.identity import AuditEntry
        with self._lock:
            seq = len(self._audit) + 1
            self._audit.append(AuditEntry(seq, at, actor, action, target, copy.deepcopy(detail)))
            return seq

    def audit(self, *, limit: int = 100, before: int | None = None, target: str | None = None) -> list[Any]:
        if not 1 <= limit <= 500:
            raise ValueError("limit 取 1–500")
        with self._lock:
            rows = [e for e in reversed(self._audit) if (before is None or e.seq < before)
                    and (target is None or e.target == target)]
            return [replace(e, detail=copy.deepcopy(e.detail)) for e in rows[:limit]]


_CHINA = timezone(timedelta(hours=8))


class InMemoryPermissionRepo:
    """PermissionRepo 的内存实现（批次 16a）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revision = 0
        self._acl: dict[str, frozenset[str]] = {}
        self._overrides: dict[str, frozenset[str]] = {}
        self._history: list[dict[str, Any]] = []
        self._indexed: dict[str, tuple[str, ...]] = {}

    def revision(self) -> int:
        with self._lock:
            return self._revision

    def folder_acl(self) -> dict[str, frozenset[str]]:
        with self._lock:
            return dict(self._acl)

    def _log(self, kind: str, target: str, groups: frozenset[str] | None, actor: str, reason: str | None,
             at: datetime) -> int:
        self._revision += 1
        self._history.append({"seq": len(self._history) + 1, "at": at.astimezone(_CHINA).isoformat(),
                              "actor": actor, "kind": kind,
                              "target": target, "groups": None if groups is None else sorted(groups),
                              "reason": reason})
        return self._revision

    def set_folder_acl(self, path: str, groups: frozenset[str] | None, *, actor: str, at: datetime) -> int:
        with self._lock:
            if groups is None:
                self._acl.pop(path, None)
            else:
                self._acl[path] = frozenset(groups)
            return self._log("folder", path, groups, actor, None, at)

    def overrides(self) -> dict[str, frozenset[str]]:
        with self._lock:
            return dict(self._overrides)

    def set_override(self, version_id: str, groups: frozenset[str] | None, *, actor: str, reason: str,
                     at: datetime) -> int:
        with self._lock:
            if groups is None:
                self._overrides.pop(version_id, None)
            else:
                self._overrides[version_id] = frozenset(groups)
            return self._log("document", version_id, groups, actor, reason, at)

    def history(self, *, target: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit 取 1–500")
        with self._lock:
            rows = [row for row in reversed(self._history) if target is None or row["target"] == target]
            return copy.deepcopy(rows[:limit])

    def references(self, group_id: str) -> bool:
        with self._lock:
            return any(group_id in groups for groups in [*self._acl.values(), *self._overrides.values()])

    def indexed_tags(self) -> dict[str, tuple[str, ...]]:
        with self._lock:
            return dict(self._indexed)

    def record_indexed(self, tags: Mapping[str, Sequence[str]]) -> None:
        with self._lock:
            self._indexed.update((version, tuple(sorted(values))) for version, values in tags.items())


class InMemoryConversationRepo:
    """ConversationRepo 的内存实现（批次 19，测试与开放模式下的进程内使用）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[str, tuple[Any, list[dict[str, Any]]]] = {}

    def create(self, conversation_id, owner, title, at):
        with self._lock:
            if conversation_id in self._rows:
                raise ValueError("对话已存在")
            summary = ConversationSummary(conversation_id, owner, title, at, at, 0)
            self._rows[conversation_id] = (summary, [])
            return summary

    def get(self, conversation_id):
        with self._lock:
            row = self._rows.get(conversation_id)
            return None if row is None else Conversation(row[0], tuple(copy.deepcopy(row[1])))

    def summaries(self, owner, *, limit=50, offset=0):
        with self._lock:
            rows = sorted((row[0] for row in self._rows.values() if row[0].owner == owner),
                          key=lambda s: (s.updated_at, s.conversation_id), reverse=True)
            return rows[offset:offset + limit]

    def count(self, owner):
        with self._lock:
            return sum(row[0].owner == owner for row in self._rows.values())

    def append(self, conversation_id, turn, at):
        with self._lock:
            summary, turns = self._rows[conversation_id]
            turns.append(copy.deepcopy(dict(turn)))
            summary = replace(summary, updated_at=at, turns=len(turns))
            self._rows[conversation_id] = (summary, turns)
            return summary

    def rename(self, conversation_id, title, at):
        with self._lock:
            summary, turns = self._rows[conversation_id]
            summary = replace(summary, title=title, updated_at=at)
            self._rows[conversation_id] = (summary, turns)
            return summary

    def delete(self, conversation_id):
        with self._lock:
            return self._rows.pop(conversation_id, None) is not None

    def scrub_versions(self, version_ids):
        from core.domain.purge import scrub_turn
        count = 0
        with self._lock:
            for summary, turns in self._rows.values():
                for index, turn in enumerate(turns):
                    new = scrub_turn(turn, version_ids)
                    if new is not None:
                        turns[index] = new
                        count += 1
            return count


class InMemoryRebuildRepo:
    """RebuildRepo 的内存实现（批次 21b，测试与 JSON 模式用）。"""

    _FIELDS = {"status", "compare", "compare_revision", "switched_at", "previous_dataset_id"}

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[str, Rebuild] = {}
        self._docs: dict[str, dict[str, BuiltDoc]] = {}

    def rebuilds(self) -> list[Rebuild]:
        with self._lock:
            return list(self._rows.values())

    def get(self, rebuild_id: str) -> Rebuild | None:
        with self._lock:
            return self._rows.get(rebuild_id)

    def _check_building(self, rebuild_id: str, status: str) -> None:
        if status == "building" and any(r.status == "building" and r.rebuild_id != rebuild_id
                                        for r in self._rows.values()):
            raise ValueError("已有正在进行的重建")

    def create(self, rebuild: Rebuild) -> None:
        with self._lock:
            if rebuild.rebuild_id in self._rows or any(r.dataset_id == rebuild.dataset_id for r in self._rows.values()):
                raise ValueError("重建记录或数据集已登记")
            self._check_building(rebuild.rebuild_id, rebuild.status)
            self._rows[rebuild.rebuild_id] = rebuild
            self._docs[rebuild.rebuild_id] = {}

    def update(self, rebuild_id: str, **fields: Any) -> Rebuild:
        with self._lock:
            if rebuild_id not in self._rows:
                raise KeyError(rebuild_id)
            if set(fields) - self._FIELDS:
                raise ValueError("不能修改这些字段：" + ", ".join(sorted(set(fields) - self._FIELDS)))
            if "status" in fields:
                self._check_building(rebuild_id, fields["status"])
            self._rows[rebuild_id] = replace(self._rows[rebuild_id], **copy.deepcopy(fields))
            return self._rows[rebuild_id]

    def docs(self, rebuild_id: str) -> dict[str, BuiltDoc]:
        with self._lock:
            return dict(self._docs.get(rebuild_id, {}))

    def _bump(self, rebuild_id: str) -> None:
        if rebuild_id not in self._rows:
            raise KeyError(rebuild_id)
        self._rows[rebuild_id] = replace(self._rows[rebuild_id], revision=self._rows[rebuild_id].revision + 1)

    def put_doc(self, rebuild_id: str, version_id: str, doc: BuiltDoc) -> None:
        with self._lock:
            self._bump(rebuild_id)
            self._docs[rebuild_id][version_id] = BuiltDoc(doc.engine_doc_id, dict(doc.chunk_map), doc.signature)

    def drop_doc(self, rebuild_id: str, version_id: str) -> None:
        with self._lock:
            self._bump(rebuild_id)
            self._docs[rebuild_id].pop(version_id, None)

    def replace_docs(self, rebuild_id: str, docs: Mapping[str, BuiltDoc]) -> None:
        with self._lock:
            self._bump(rebuild_id)
            self._docs[rebuild_id] = {v: BuiltDoc(d.engine_doc_id, dict(d.chunk_map), d.signature) for v, d in docs.items()}
