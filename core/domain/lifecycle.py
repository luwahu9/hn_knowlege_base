"""文档版本与撤下的纯逻辑（批次 20a）。规格见 docs/spec-36-document-lifecycle.md。

- 同一文档：文件夹（规范路径）相同、文件名（NFC 后 casefold）相同的记录。网页上传与目录扫描一样，同名即新版本。
- 状态：在原有导入状态之外增加 withdrawn（已撤下）与 superseded（已替代）。只有 published、withdrawn、superseded 三者之间
  可以互相转换；导入失败、质检未通过、重复等记录不参与。
- 两个以上已发布版本属于同一文档时为“版本待确认”：两版都能被检索，直到审核人确认保留哪一个（其余变为已替代）。
"""
from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

PUBLISHED, WITHDRAWN, SUPERSEDED = "published", "withdrawn", "superseded"
LIFECYCLE_STATUSES = frozenset({PUBLISHED, WITHDRAWN, SUPERSEDED})
MANUAL, SOURCE_MISSING, REPLACED = "manual", "source_missing", "replaced"     # 撤下或替代的原因
MAX_REASON_CHARS = 200


def document_key(record: Mapping[str, Any]) -> tuple[str, str]:
    """(文件夹规范路径, NFC 后 casefold 的文件名)。"""
    return record.get("folder", ""), unicodedata.normalize("NFC", record["filename"]).casefold()


def same_document(records: Iterable[Mapping[str, Any]], record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """与 record 属于同一文档的全部记录（含它自己），保持 records 的顺序（即保存先后）。"""
    key = document_key(record)
    return [row for row in records if document_key(row) == key]


def version_groups(records: Iterable[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """版本待确认：同一文档有两个以上已发布记录的组。组内按保存先后；各组按组内最后一个记录的保存位置升序。"""
    groups: dict[tuple[str, str], list[tuple[int, Mapping[str, Any]]]] = {}
    for position, row in enumerate(records):
        if row["status"] == PUBLISHED:
            groups.setdefault(document_key(row), []).append((position, row))
    pending = [rows for rows in groups.values() if len(rows) > 1]
    pending.sort(key=lambda rows: rows[-1][0])
    return [[row for _, row in rows] for rows in pending]


def lifecycle_entry(action: str, *, at: str, by: str, reason: str = "", superseded_by: str | None = None) -> dict[str, Any]:
    """记录在文档的 "lifecycle" 列表中的一条：{"action": "withdraw"|"supersede"|"restore", "at", "by", "reason"}，
    替代时另有 "superseded_by"。reason 去首尾空白，超过 MAX_REASON_CHARS → ValueError。"""
    reason = reason.strip()
    if len(reason) > MAX_REASON_CHARS:
        raise ValueError(f"原因不能超过 {MAX_REASON_CHARS} 字")
    entry: dict[str, Any] = {"action": action, "at": at, "by": by, "reason": reason}
    if superseded_by is not None:
        entry["superseded_by"] = superseded_by
    return entry


def withdrawn_for_missing(record: Mapping[str, Any]) -> bool:
    """记录是否因源文件缺失而被撤下（状态为 withdrawn，且最后一条 lifecycle 是原因为 SOURCE_MISSING 的撤下）。"""
    entries = record.get("lifecycle") or []
    return (record["status"] == WITHDRAWN and bool(entries) and entries[-1]["action"] == "withdraw"
            and entries[-1]["reason"] == SOURCE_MISSING)
