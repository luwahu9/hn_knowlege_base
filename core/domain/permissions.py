"""按文件夹路径的授权计算（批次 16a）：把文档登记处的文件夹树与授权设置组装成 access_policy 的快照。
规格见 docs/spec-28-auth.md 第三节；判定规则本身见 core/domain/access_policy.py（批次 1）。

约定：文件夹 ID 就是规范文件夹路径（根为 ""）；文档 ID 就是 version_id。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from core.domain.access_policy import AclSnapshot, Document, Folder, Principal, effective_groups
from core.domain.folders import folder_ancestors, folder_parent, in_folder

NOBODY_TAG = "none"     # 有效用户组为空的文档写入检索索引时使用的标签；不是任何组的 ID，因此谁也检索不到


def folder_snapshot(folder_paths: Iterable[str], folder_acl: Mapping[str, frozenset[str]],
                    overrides: Mapping[str, frozenset[str]]) -> AclSnapshot:
    """组装快照：folders 含根 ""、folder_paths 中的每个路径及其全部祖先（parent 为 folder_parent）。
    folder_acl 中的路径不在树中也保留（例如文件夹尚无文档时先授权）——那些路径的祖先同样补进树里。
    路径必须已是规范形式，否则 ValueError（来自 folders 模块）。返回的映射是新对象。"""
    paths = {""}
    for path in list(folder_paths) + list(folder_acl):
        paths.update(folder_ancestors(path))
    folders = {path: Folder(path, folder_parent(path)) for path in paths}
    return AclSnapshot(folders, {path: frozenset(groups) for path, groups in folder_acl.items()},
                       {version: frozenset(groups) for version, groups in overrides.items()})


def document_groups(version_id: str, folder: str, snap: AclSnapshot) -> frozenset[str]:
    """文档的有效用户组（单文档例外优先，其次最近的有授权设置的文件夹，都没有 → 空集合）。
    folder 不在快照的树中（例如快照取得之后才新建的文件夹）时，按补上该路径后的树计算，不报错。"""
    if folder not in snap.folders and version_id not in snap.doc_override:
        snap = folder_snapshot([folder, *snap.folders], snap.folder_acl, snap.doc_override)
    return effective_groups(Document(version_id, folder), snap)


def readable(principal: Principal, version_id: str, folder: str, snap: AclSnapshot) -> bool:
    return bool(principal.group_ids & document_groups(version_id, folder, snap))


def readable_versions(principal: Principal, documents: Iterable[tuple[str, str]], snap: AclSnapshot) -> frozenset[str]:
    """documents 为 (version_id, folder) 序列，返回其中 principal 可读的 version_id 集合。"""
    return frozenset(version for version, folder in documents if readable(principal, version, folder, snap))


def folder_readable(principal: Principal, folder: str, snap: AclSnapshot) -> bool:
    """principal 能否读取“放在该文件夹里、没有单文档例外的文档”，即能否向该文件夹上传。
    folder 不必已存在于树中：按它最近的已存在祖先的授权计算。"""
    return bool(principal.group_ids & document_groups("\0probe", folder,
                                                      AclSnapshot(snap.folders, snap.folder_acl, {})))


def index_tags(version_id: str, folder: str, snap: AclSnapshot) -> tuple[str, ...]:
    """写入检索索引的用户组标签：有效用户组排序后的元组；为空时为 (NOBODY_TAG,)。"""
    groups = document_groups(version_id, folder, snap)
    return tuple(sorted(groups)) if groups else (NOBODY_TAG,)


@dataclass(frozen=True)
class Visibility:
    """一个用户能读的文档范围（批次 16b），用于把权限过滤交给查询（含数据库查询）执行：
    版本 v（位于文件夹 f）可读 ⇔ v ∈ allow，或（f ∈ folders 且 v ∉ deny）。
    folders：没有单文档例外时可读的文件夹（规范路径）；allow / deny：有单文档例外且可读 / 不可读的版本。
    快照之后才出现的文件夹不在 folders 中，按不可读处理（宁缺毋滥，下一次计算时纳入）。"""
    folders: frozenset[str]
    allow: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()

    def contains(self, version_id: str, folder: str) -> bool:
        if version_id in self.allow:
            return True
        return version_id not in self.deny and folder in self.folders


def visibility_for(principal: Principal, snap: AclSnapshot) -> Visibility:
    """由快照算出 principal 的 Visibility：folders 为快照树中 folder_readable 为真的文件夹；
    单文档例外中与 principal 的组有交集的进 allow，其余进 deny。"""
    no_overrides = AclSnapshot(snap.folders, snap.folder_acl, {})
    folders = frozenset(path for path in snap.folders
                        if principal.group_ids & effective_groups(Document("\0probe", path), no_overrides))
    allow = frozenset(version for version, groups in snap.doc_override.items() if principal.group_ids & groups)
    deny = frozenset(version for version in snap.doc_override if version not in allow)
    return Visibility(folders, allow, deny)


@dataclass(frozen=True)
class FolderScope:
    """按文件夹判定的可读范围（批次 17a，邮件使用；邮件没有单文档例外）：
    文件夹 f 可读 ⇔ 存在 (g, excluded) ∈ grants，f 位于 g 之内（含 g 自身，folders.in_folder），
    且 f 不在 excluded 中任何文件夹之内。g 与 excluded 都是规范路径（根为 ""）。
    grants 按 g 升序，excluded 各自升序。与快照所含文件夹树无关：任意深度、尚未登记的文件夹都能判定。"""
    grants: tuple[tuple[str, tuple[str, ...]], ...]

    def contains(self, folder: str) -> bool:
        return any(in_folder(folder, granted) and not any(in_folder(folder, item) for item in excluded)
                   for granted, excluded in self.grants)


def folder_scope(principal: Principal, snap: AclSnapshot) -> FolderScope:
    """principal 的 FolderScope，与“文件夹中没有例外的文档是否可读”（folder_readable）的判定完全一致：
    文件夹的有效授权取最近的有显式设置的祖先（含自身、含根 ""），都没有 → 不可读。

    构造：显式设置中与 principal 的组有交集的文件夹为授权点 g，没有交集的（含空集合）为拒绝点 d。
    - 每个授权点 g 的 excluded = 严格位于 g 之内的拒绝点，只保留最浅的那些（某个拒绝点位于另一个已保留的拒绝点之内时省去）；
    - 省去冗余的授权点：g 严格位于另一个授权点 g0 之内，且 g0 与 g 之间（严格位于 g0 之内、g 位于其内）没有拒绝点时，g 不单列。
    结果只取决于 snap.folder_acl，与 snap.folders、snap.doc_override 无关。"""
    granted = sorted(path for path, groups in snap.folder_acl.items() if principal.group_ids & groups)
    denied = sorted(path for path, groups in snap.folder_acl.items() if not principal.group_ids & groups)

    def strictly_inside(path: str, folder: str) -> bool:
        return path != folder and in_folder(path, folder)

    grants: list[tuple[str, tuple[str, ...]]] = []
    for path in granted:
        redundant = any(strictly_inside(path, outer) and not any(strictly_inside(d, outer) and in_folder(path, d)
                                                                 for d in denied)
                        for outer in granted)
        if redundant:
            continue
        inner = [d for d in denied if strictly_inside(d, path)]
        excluded = tuple(d for d in inner if not any(strictly_inside(d, other) for other in inner))
        grants.append((path, excluded))
    return FolderScope(tuple(grants))
