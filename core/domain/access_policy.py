"""权限策略（纯逻辑）。规格：docs/spec-03-access-policy.md

文件夹授权为主，单个文档可覆盖；默认拒绝；管理员没有读取豁免。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    user_id: str
    group_ids: frozenset[str]


@dataclass(frozen=True)
class Folder:
    id: str
    parent_id: str | None


@dataclass(frozen=True)
class Document:
    id: str
    folder_id: str


@dataclass(frozen=True)
class AclSnapshot:
    """权限数据快照。

    folders：文件夹 id → Folder
    folder_acl：文件夹 id → 用户组集合。键存在即“显式设置”，空集合表示明确拒绝所有人并截断继承；
                键不存在表示继承上级。
    doc_override：文档 id → 用户组集合。键存在即覆盖（替换，不是合并），空集合表示无人可读。
    """

    folders: Mapping[str, Folder]
    folder_acl: Mapping[str, frozenset[str]]
    doc_override: Mapping[str, frozenset[str]]


def effective_groups(doc: Document, snap: AclSnapshot) -> frozenset[str]:
    """文档的有效用户组。

    1. doc.id 在 doc_override 中 → 直接返回该集合。
    2. 否则从 doc.folder_id 向上找到第一个在 folder_acl 中有键的文件夹，返回其集合。
    3. 一直到根都没有显式设置 → 返回空集合（默认拒绝）。
    folder_id 或链上任何 parent_id 在 snap.folders 中不存在 → LookupError；
    文件夹链出现环 → ValueError。
    """
    if doc.id in snap.doc_override:
        return snap.doc_override[doc.id]
    for folder_id in _folder_chain(doc.folder_id, snap):
        if folder_id in snap.folder_acl:
            return snap.folder_acl[folder_id]
    return frozenset()


def can_read(principal: Principal, doc: Document, snap: AclSnapshot) -> bool:
    """principal 的用户组与文档有效用户组有交集才可读。"""
    return bool(principal.group_ids & effective_groups(doc, snap))


def group_tags(doc: Document, snap: AclSnapshot) -> tuple[str, ...]:
    """写入检索索引的用户组标签：有效用户组排序后的元组。"""
    return tuple(sorted(effective_groups(doc, snap)))


def can_see_edge(
    principal: Principal, src_groups: frozenset[str], dst_groups: frozenset[str]
) -> bool:
    """边可见当且仅当两端文档对该用户都可读。"""
    return bool(principal.group_ids & src_groups) and bool(principal.group_ids & dst_groups)


def derived_groups(sources: Iterable[frozenset[str]]) -> frozenset[str]:
    """衍生数据（摘要、连线理由、对话历史等）的用户组：所有来源有效用户组的交集。
    来源为空时返回空集合（默认拒绝）。"""
    iterator = iter(sources)
    groups = next(iterator, frozenset())
    for source in iterator:
        groups = groups & source
    return groups


def docs_affected_by_folder_change(
    folder_id: str, snap: AclSnapshot, documents: Iterable[Document]
) -> set[str]:
    """文件夹 folder_id 的权限变更后，需要重新计算索引标签的文档 id 集合。

    文档受影响当且仅当：没有 doc_override，且从其文件夹向上走，在遇到任何
    “有显式 ACL 的其他文件夹”之前先到达了 folder_id（folder_id 本身也算在链上）。
    链上存在环或文件夹缺失时的行为同 effective_groups。
    """
    affected: set[str] = set()
    for doc in documents:
        if doc.id in snap.doc_override:
            continue
        for current in _folder_chain(doc.folder_id, snap):
            if current == folder_id:
                affected.add(doc.id)
                break
            if current in snap.folder_acl:
                break
    return affected


def _folder_chain(folder_id: str, snap: AclSnapshot) -> Iterable[str]:
    """惰性遍历文件夹链，仅校验实际访问到的文件夹。"""
    visited: set[str] = set()
    current: str | None = folder_id
    while current is not None:
        if current not in snap.folders:
            raise LookupError(f"文件夹不存在：{current}")
        if current in visited:
            raise ValueError(f"文件夹链存在环：{current}")
        visited.add(current)
        folder = snap.folders[current]
        yield current
        current = folder.parent_id
