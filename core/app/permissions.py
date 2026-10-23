"""文件夹授权：端口与用例（批次 16a）。规格见 docs/spec-28-auth.md 第三节。

授权的对象是用户组（含内置 ALL_GROUP）；文件夹 ID 即规范路径，文档 ID 即 version_id。
判定规则沿用批次 1 的 access_policy：显式设置优先、空集合截断继承、默认拒绝、单文档例外整体替换、管理员没有阅读豁免。
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import datetime
from threading import RLock
from typing import Any, Protocol, runtime_checkable

from core.app.registry import DocumentRegistry
from core.domain.access_policy import AclSnapshot, Principal, docs_affected_by_folder_change
from core.domain.access_policy import Document as AclDocument
from core.domain.folders import normalize_folder_path
from core.app.identity import Forbidden
from core.domain.identity import ALL_GROUP, Action, User, allowed
from core.domain.permissions import (FolderScope, Visibility, folder_readable, folder_scope, folder_snapshot,
                                     index_tags, readable, visibility_for)

REASON_MAX = 500


@runtime_checkable
class PermissionRepo(Protocol):
    """授权设置、检索索引标签记录与授权审计。返回值都是副本。"""

    def revision(self) -> int:
        """授权设置的修订号：每次 set_folder_acl / set_override 成功后加一（用于缓存失效与同步任务的幂等键）。初始为 0。"""

    def folder_acl(self) -> dict[str, frozenset[str]]:
        """{规范路径: 用户组集合}，只含有显式设置的文件夹。"""

    def set_folder_acl(self, path: str, groups: frozenset[str] | None, *, actor: str, at: datetime) -> int:
        """groups 为 None 表示取消显式设置（改为继承）；空集合表示明确拒绝所有人并截断继承。
        写审计 {"kind": "folder", "target": path, "groups": 排序后的列表或 None}，返回新的修订号。"""

    def overrides(self) -> dict[str, frozenset[str]]:
        """{version_id: 用户组集合}。"""

    def set_override(self, version_id: str, groups: frozenset[str] | None, *, actor: str, reason: str,
                     at: datetime) -> int:
        """None 表示取消例外。写审计 {"kind": "document", "target": version_id, "groups", "reason"}，返回新的修订号。"""

    def history(self, *, target: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """授权审计，最新的在前：[{"seq","at","actor","kind","target","groups","reason"}]。
        at：写入时刻换算到 UTC+08:00 后的 isoformat（与任务队列等接口一致；写入时给的是其他时区也统一换算）。
        target 给出时只取该对象。limit 取 1–500，否则 ValueError。"""

    def references(self, group_id: str) -> bool:
        """该组是否仍出现在任何文件夹授权或单文档例外中。"""

    def indexed_tags(self) -> dict[str, tuple[str, ...]]:
        """{version_id: 最近一次成功写入检索索引的标签（排序后的元组）}。"""

    def record_indexed(self, tags: Mapping[str, Sequence[str]]) -> None:
        """记录这些版本已写入索引的标签（整体替换各自的记录）。"""


class PermissionService:
    """把登记处的文件夹树与 PermissionRepo 组合起来做判定。

    - snapshot()：folder_snapshot(registry.folders(), repo.folder_acl(), repo.overrides())；
      按 (repo.revision(), len(registry.folders())) 缓存，二者不变时返回同一个快照对象。
    - can_read(principal, row) / filter_rows(principal, rows)：row 为登记处的文档记录（取 "version_id"、"folder"，
      folder 缺省为 ""）。
    - readable_versions(principal)：登记处全部文档中可读的 version_id 集合。
    - can_upload(principal, folder)：folder_readable（向该文件夹上传 = 能读该文件夹中没有例外的文档）。
    - tags(version_id, folder)：index_tags。
    - set_folder_acl(actor, path, groups) / set_override(actor, version_id, groups, reason)：
      actor 不允许 MANAGE_ACL → Forbidden（core.app.identity.Forbidden）；
      path 先规范化（非法 → ValueError）；version_id 不在登记处 → KeyError；
      groups 中有不在 known_groups() 的 ID → ValueError；reason 去空白后为空或超过 REASON_MAX → ValueError。
      成功后调用 on_change(新修订号)，并返回受影响的已发布 version_id 升序列表
      （文件夹：docs_affected_by_folder_change；单文档：该版本，若已发布）。
    - stale(limit)：已发布文档中，当前应有的标签与 repo.indexed_tags() 记录不同（或没有记录）的
      [(version_id, 应有的标签)]，按 version_id 升序，最多 limit 条。"""

    def __init__(self, registry: DocumentRegistry, repo: PermissionRepo, *, known_groups: Callable[[], Collection[str]],
                 clock: Callable[[], datetime], on_change: Callable[[int], Any] = lambda revision: None) -> None:
        self._registry, self._repo = registry, repo
        self._known, self._clock, self._on_change = known_groups, clock, on_change
        self._lock = RLock()
        self._cached: tuple[tuple[int, int], AclSnapshot] | None = None
        self._scope_snap: AclSnapshot | None = None
        self._scopes: dict[frozenset[str], Visibility] = {}
        self._folder_scopes: dict[frozenset[str], FolderScope] = {}

    @property
    def repo(self) -> PermissionRepo:
        return self._repo

    def set_on_change(self, callback: Callable[[int], Any]) -> None:
        """替换授权变更后的回调（Web 装配时接到标签同步作业的 enqueue）。"""
        self._on_change = callback

    def ensure_default(self) -> bool:
        """首次启用账号时的默认授权（批次 16a 用户确认）：修订号为 0（从未设置过任何授权）时把根文件夹授权给
        ALL_GROUP（actor "system"），返回 True；否则什么都不做，返回 False。"""
        if self._repo.revision() != 0:
            return False
        revision = self._repo.set_folder_acl("", frozenset({ALL_GROUP}), actor="system", at=self._clock())
        self._on_change(revision)
        return True

    def snapshot(self) -> AclSnapshot:
        folders = self._registry.folders()
        key = (self._repo.revision(), len(folders))
        with self._lock:
            if self._cached is not None and self._cached[0] == key:
                return self._cached[1]
        snap = folder_snapshot(folders, self._repo.folder_acl(), self._repo.overrides())
        with self._lock:
            self._cached = (key, snap)
        return snap

    def visibility(self, principal: Principal) -> Visibility:
        """principal 的可读范围（批次 16b，visibility_for）。按 (当前快照, 用户组集合) 缓存，快照变化时清空。"""
        snap = self.snapshot()
        with self._lock:
            if self._scope_snap is snap and principal.group_ids in self._scopes:
                return self._scopes[principal.group_ids]
        scope = visibility_for(principal, snap)
        with self._lock:
            if self._scope_snap is not snap:
                self._scope_snap, self._scopes, self._folder_scopes = snap, {}, {}
            self._scopes[principal.group_ids] = scope
        return scope

    def folder_scope(self, principal: Principal) -> FolderScope:
        """批次 17a：principal 按文件夹的可读范围（邮件使用，folder_scope）。按 (当前快照, 用户组集合) 缓存，快照变化时清空。"""
        snap = self.snapshot()
        with self._lock:
            if self._scope_snap is snap and principal.group_ids in self._folder_scopes:
                return self._folder_scopes[principal.group_ids]
        scope = folder_scope(principal, snap)
        with self._lock:
            if self._scope_snap is not snap:
                self._scope_snap, self._scopes, self._folder_scopes = snap, {}, {}
            self._folder_scopes[principal.group_ids] = scope
        return scope

    def can_read(self, principal: Principal, row: Mapping[str, Any], snap: AclSnapshot | None = None) -> bool:
        return readable(principal, row["version_id"], row.get("folder", ""), snap or self.snapshot())

    def filter_rows(self, principal: Principal, rows: Iterable[Mapping[str, Any]]) -> list[Any]:
        """批次 16b 起用 visibility 判断（集合查找），结果与逐条 can_read 相同，只是快照之后新出现的文件夹按不可读处理。"""
        scope = self.visibility(principal)
        return [row for row in rows if scope.contains(row["version_id"], row.get("folder", ""))]

    def readable_versions(self, principal: Principal) -> frozenset[str]:
        snap = self.snapshot()
        return frozenset(row["version_id"] for row in self._registry.documents() if self.can_read(principal, row, snap))

    def can_upload(self, principal: Principal, folder: str) -> bool:
        return folder_readable(principal, normalize_folder_path(folder), self.snapshot())

    def tags(self, version_id: str, folder: str) -> tuple[str, ...]:
        return index_tags(version_id, folder, self.snapshot())

    def _check(self, actor: User, groups: frozenset[str] | None) -> frozenset[str] | None:
        if not actor.active or not allowed(actor.role, Action.MANAGE_ACL):
            raise Forbidden("没有执行该操作的权限")
        if groups is None:
            return None
        groups = frozenset(groups)
        unknown = groups - frozenset(self._known())
        if unknown:
            raise ValueError("用户组不存在：" + "、".join(sorted(unknown)))
        return groups

    def _published(self) -> list[dict[str, Any]]:
        return [row for row in self._registry.documents() if row["status"] == "published"]

    def set_folder_acl(self, actor: User, path: str, groups: frozenset[str] | None) -> list[str]:
        groups = self._check(actor, groups)
        path = normalize_folder_path(path)
        revision = self._repo.set_folder_acl(path, groups, actor=actor.user_id, at=self._clock())
        snap = self.snapshot()
        published = self._published()
        affected = docs_affected_by_folder_change(
            path, snap, [AclDocument(row["version_id"], row.get("folder", "")) for row in published])
        self._on_change(revision)
        return sorted(affected)

    def set_override(self, actor: User, version_id: str, groups: frozenset[str] | None, reason: str) -> list[str]:
        groups = self._check(actor, groups)
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason or len(reason) > REASON_MAX:
            raise ValueError(f"必须填写原因（不超过 {REASON_MAX} 字）")
        detail = self._registry.detail(version_id)
        if detail is None:
            raise KeyError(version_id)
        revision = self._repo.set_override(version_id, groups, actor=actor.user_id, reason=reason, at=self._clock())
        self._on_change(revision)
        return [version_id] if detail["document"]["status"] == "published" else []

    def stale(self, limit: int = 1000) -> list[tuple[str, tuple[str, ...]]]:
        snap = self.snapshot()
        indexed = self._repo.indexed_tags()
        result = []
        for row in sorted(self._published(), key=lambda row: row["version_id"]):
            tags = index_tags(row["version_id"], row.get("folder", ""), snap)
            if indexed.get(row["version_id"]) != tags:
                result.append((row["version_id"], tags))
                if len(result) >= limit:
                    break
        return result


@runtime_checkable
class IndexTagger(Protocol):
    """导入发布时取检索索引标签，并在发布成功后记录。"""

    def tags(self, version_id: str, folder: str) -> tuple[str, ...]: ...

    def record(self, version_id: str, tags: Sequence[str]) -> None: ...


class LegacyTagger:
    """未启用账号（开放模式）：所有文档用登记处的固定标签（批次 7a 起的 group_tag），不做记录。"""

    def __init__(self, tag: str) -> None:
        self._tag = tag

    def tags(self, version_id: str, folder: str) -> tuple[str, ...]:
        return (self._tag,)

    def record(self, version_id: str, tags: Sequence[str]) -> None:
        return None


class PermissionTagger:
    """启用账号：标签按文件夹授权计算（PermissionService.tags），发布后记入 repo.record_indexed。"""

    def __init__(self, service: PermissionService) -> None:
        self._service = service

    def tags(self, version_id: str, folder: str) -> tuple[str, ...]:
        return self._service.tags(version_id, folder)

    def record(self, version_id: str, tags: Sequence[str]) -> None:
        self._service.repo.record_indexed({version_id: tuple(tags)})
