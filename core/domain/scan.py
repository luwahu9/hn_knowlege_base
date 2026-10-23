"""目录扫描与文件夹上传的纯逻辑（批次 15d）：相对路径拆分、候选文件筛选、增量扫描计划。规格见 docs/spec-27-folder-import.md。

不触碰文件系统：文件的大小、修改时间由调用方给出。
"""
from __future__ import annotations

import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from core.domain.folders import normalize_folder_path

# 扫描结果的种类
NEW = "new"                 # 新文件，已提交导入
CHANGED = "changed"         # 已扫描过但内容变了，已提交导入（生成新版本）
UNCHANGED = "unchanged"     # 与上次相同（大小与修改时间相同，或内容哈希相同）
DUPLICATE = "duplicate"     # 内容与已登记的文档相同，未导入
REJECTED = "rejected"       # 提交时被拒绝（文件名不合法等），未导入
FAILED = "failed"           # 读取失败，下次扫描重试
SKIPPED = "skipped"         # 不是候选文件（见 skip_reason），不读取
MISSING = "missing"         # 上次还在、这次不见了（只报告，不删除文档）
OUTCOMES = (NEW, CHANGED, UNCHANGED, DUPLICATE, REJECTED, FAILED, SKIPPED, MISSING)

# 跳过原因
HIDDEN = "hidden"               # 某一段以 "." 或 "~$" 开头（隐藏文件、Office 临时文件）
UNSUPPORTED = "unsupported"     # 扩展名不在支持清单内
EMPTY = "empty"                 # 0 字节
TOO_LARGE = "too_large"         # 超过大小上限
INVALID_PATH = "invalid_path"   # 路径无法规范化（禁用字符、层级过深等）


@dataclass(frozen=True)
class FileStat:
    """扫描到的一个文件。path：相对扫描根的路径，"/" 分隔、NFC、不以 "/" 开头。"""
    path: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class ScanEntry:
    """扫描索引中一个文件的最近状态。outcome 只会是 NEW、CHANGED、UNCHANGED、DUPLICATE、REJECTED 之一
    （FAILED、SKIPPED 的文件不进索引）。version_id：导入或重复时对应的文档版本，REJECTED 为 None。"""
    path: str
    size: int
    mtime_ns: int
    sha256: str
    version_id: str | None
    outcome: str


@dataclass(frozen=True)
class ScanPlan:
    """plan_scan 的结果，各元组按 path 升序。
    unchanged：大小与修改时间都和索引相同，不必读取；
    to_read：需要读取并计算哈希的文件（新文件，或大小/修改时间有变化）；
    skipped：(path, 原因)；missing：索引里有、本次没扫到的路径（含本次变为非候选的文件）。"""
    unchanged: tuple[str, ...]
    to_read: tuple[FileStat, ...]
    skipped: tuple[tuple[str, str], ...]
    missing: tuple[str, ...]


def split_import_path(path: str) -> tuple[str, str]:
    """把导入用的相对路径（浏览器的 webkitRelativePath 或扫描到的相对路径）拆成 (规范文件夹, 文件名)。

    - "\\" 视同 "/"；整条路径按 normalize_folder_path 规范化（非法 → ValueError）；
    - 规范化后为空 → ValueError；最后一段为文件名，其余为文件夹（可以是根 ""）。
    例："周报\\2026年\\3月.md" → ("周报/2026年", "3月.md")。"""
    normalized = normalize_folder_path(path)
    if not normalized:
        raise ValueError("导入路径为空")
    folder, _, filename = normalized.rpartition("/")
    return folder, filename


def join_folder(root: str, folder: str) -> str:
    """扫描根名称与相对文件夹拼成文档的文件夹：join_folder("共享盘", "南海/2026") → "共享盘/南海/2026"；
    folder 为 "" 时就是 root。两者都必须已是规范形式，root 不能为空，否则 ValueError。"""
    if not root or normalize_folder_path(root) != root or normalize_folder_path(folder) != folder:
        raise ValueError("文件夹路径必须已规范化")
    return normalize_folder_path(f"{root}/{folder}")


def skip_reason(stat: FileStat, *, extensions: Collection[str], max_bytes: int) -> str | None:
    """候选文件检查，返回跳过原因或 None。依次检查：INVALID_PATH、HIDDEN、UNSUPPORTED、EMPTY、TOO_LARGE。
    extensions：小写、不含点的扩展名集合。"""
    try:
        split_import_path(stat.path)
    except ValueError:
        return INVALID_PATH
    segments = unicodedata.normalize("NFC", stat.path.replace("\\", "/")).split("/")
    if any(part.strip().startswith((".", "~$")) for part in segments if part.strip()):
        return HIDDEN
    name = segments[-1].strip()
    extension = name.rpartition(".")[2].lower() if "." in name else ""
    if extension not in extensions:
        return UNSUPPORTED
    if stat.size == 0:
        return EMPTY
    if stat.size > max_bytes:
        return TOO_LARGE
    return None


def plan_scan(previous: Mapping[str, ScanEntry], current: Sequence[FileStat], *,
              extensions: Collection[str], max_bytes: int) -> ScanPlan:
    """增量扫描计划。current 中 path 重复 → ValueError。

    - 非候选文件（skip_reason 非 None）→ skipped；
    - 候选文件在 previous 中且 size、mtime_ns 都相同 → unchanged；
    - 其余候选文件 → to_read；
    - previous 中有、而本次候选文件中没有的 path → missing。"""
    seen: set[str] = set()
    unchanged: list[str] = []
    to_read: list[FileStat] = []
    skipped: list[tuple[str, str]] = []
    for stat in current:
        if stat.path in seen:
            raise ValueError("扫描结果中有重复路径")
        seen.add(stat.path)
    candidates: set[str] = set()
    for stat in sorted(current, key=lambda row: row.path):
        reason = skip_reason(stat, extensions=extensions, max_bytes=max_bytes)
        if reason is not None:
            skipped.append((stat.path, reason))
            continue
        candidates.add(stat.path)
        entry = previous.get(stat.path)
        if entry is not None and entry.size == stat.size and entry.mtime_ns == stat.mtime_ns:
            unchanged.append(stat.path)
        else:
            to_read.append(stat)
    missing = tuple(sorted(path for path in previous if path not in candidates))
    return ScanPlan(tuple(unchanged), tuple(to_read), tuple(skipped), missing)
