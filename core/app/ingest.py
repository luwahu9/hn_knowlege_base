"""入库用例：转换 → 质检 → 切块 → 发布。规格：docs/spec-12-e2e-retrieval.md

只依赖端口（core/app/ports.py）和领域模块，不直接依赖任何适配器。
本批不涉及版本状态机、审核和权限计算：调用方直接给出 version_id 和用户组标签。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.app.ports import (
    BlobStore,
    Chunk,
    Converter,
    ConvertResult,
    Indexer,
    IndexReceipt,
    PermanentError,
    UnsupportedFormatError,
)
from core.domain.chunker import ChunkDraft, ChunkingConfig, chunk_document, render_for_index
from core.domain.conversion_qc import QcReport, Severity, check_conversion
from core.domain.chunker import normalize_text
from core.domain.doc_period import Period, document_period
from core.domain.file_kind import with_extension
from core.domain.restructure import RestructureReport, restructure

# 这些格式转换时开启 OCR（扫描件、图片）。文本型 PDF 开启 OCR 也只识别其中的图片区域，不影响文本层。
OCR_EXTENSIONS = frozenset({"pdf", "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"})


class IngestStatus(str, Enum):
    PUBLISHED = "published"          # 已发布到检索引擎
    UNSUPPORTED = "unsupported"      # 格式不支持（转换器抛 UnsupportedFormatError），未发布
    QC_BLOCKED = "qc_blocked"        # 质检有阻断项，未发布
    NO_CHUNKS = "no_chunks"          # 质检通过但没有切出任何分块，未发布
    DUPLICATE = "duplicate"          # 正文与已发布版本相同，未发布（duplicate_of 指向该版本）


@dataclass(frozen=True)
class IngestRequest:
    version_id: str
    source_key: str              # BlobStore 中原件的键
    filename: str                # 原件文件名（含扩展名）
    title: str                   # 文档标题，进入面包屑
    group_tags: tuple[str, ...]  # 可见的用户组
    folders: tuple[str, ...] = ()  # 导入时的相对路径中的各级文件夹名（自顶向下），用于推断文档时间段
    extension: str | None = None   # 调用方按文件头确定的有效扩展名（effective_extension）；None 表示按文件名扩展名


@dataclass(frozen=True)
class IngestOutcome:
    version_id: str
    status: IngestStatus
    ocr: bool
    conversion: ConvertResult | None     # UNSUPPORTED 时为 None
    qc: QcReport | None                  # UNSUPPORTED 时为 None
    chunks: tuple[Chunk, ...]            # 发布（或本应发布）的分块；UNSUPPORTED / QC_BLOCKED 时为空
    receipt: IndexReceipt | None         # 仅 PUBLISHED 时非 None
    detail: str = ""                     # 未发布时的原因说明
    extension: str | None = None         # 按文件头确定的有效扩展名；无法识别为 None
    restructure: RestructureReport | None = None   # 结构修整报告；未转换时为 None
    content_hash: str | None = None      # 正文哈希（见 content_hash）；未切块时为 None
    period: Period | None = None         # 文档时间段；推不出或未切块时为 None
    duplicate_of: str | None = None      # 仅 DUPLICATE：正文相同的已发布版本


def extension_of(filename: str) -> str:
    """小写扩展名，不含“.”；没有扩展名返回空串。"""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def chunk_id_for(version_id: str, index: int) -> str:
    """稳定的分块 ID：<version_id>:<5 位序号>。"""
    return f"{version_id}:{index:05d}"


_URL = re.compile(r"https?://[^\s<>\"'，。；、）)】]+")


def chunk_sources(text: str) -> list[str]:
    """正文中出现的 http/https 网址，按出现顺序去重；末尾的 .,;:! 等标点不算网址的一部分。"""
    found: list[str] = []
    for match in _URL.finditer(text):
        url = match.group(0).rstrip(".,;:!?")
        if url not in found:
            found.append(url)
    return found


def content_hash(drafts: Sequence[ChunkDraft]) -> str:
    """正文哈希：各分块草稿 text 经 normalize_text 后去掉全部空白，按顺序用换行连接，取 UTF-8 的 SHA-256 十六进制。
    不含标题与面包屑，因此文件名、标题不同而正文相同的文档哈希相同。"""
    joined = "\n".join("".join(normalize_text(draft.text).split()) for draft in drafts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _head_lines(doc: Any, limit: int = 5) -> list[str]:
    """标题式的开头几行：按 texts 顺序取 content_layer 为 body、规范化后非空的前 limit 个文字节点，
    只保留其中长度 ≤ 40 且不含“。！？”的（正文句子中的日期不用来推断文档时间段）。"""
    lines: list[str] = []
    seen = 0
    for node in doc.get("texts", []):
        if node.get("content_layer", "body") != "body":
            continue
        text = normalize_text(node.get("text", ""))
        if not text:
            continue
        seen += 1
        if len(text) <= 40 and not any(mark in text for mark in "。！？"):
            lines.append(text)
        if seen >= limit:
            break
    return lines


def to_chunks(version_id: str, drafts: Sequence[ChunkDraft], *, title: str) -> tuple[Chunk, ...]:
    """把分块草稿转换成端口的 Chunk。

    - chunk_id = chunk_id_for(version_id, draft.index)
    - text = render_for_index(draft, title=title)
    - page_start / page_end 照搬
    - section_path = " > ".join(draft.section_path)，空路径为 None
    - meta = {"kind": draft.kind}；表格被拆分时再加 "table_part": "i/n"；
      正文中出现网址（chunk_sources）时再加 "sources": [网址…]
    """
    chunks: list[Chunk] = []
    for draft in drafts:
        meta = {"kind": draft.kind}
        if draft.table_part is not None:
            part, total = draft.table_part
            meta["table_part"] = f"{part}/{total}"
        sources = chunk_sources(draft.text)
        if sources:
            meta["sources"] = sources
        chunks.append(Chunk(
            chunk_id=chunk_id_for(version_id, draft.index),
            text=render_for_index(draft, title=title),
            page_start=draft.page_start, page_end=draft.page_end,
            section_path=" > ".join(draft.section_path) or None, meta=meta,
        ))
    return tuple(chunks)


class Ingestor:
    def __init__(self, blobs: BlobStore, converter: Converter, indexer: Indexer, *,
                 config_hash: str, chunking: ChunkingConfig | None = None,
                 find_duplicate: Callable[[str], str | None] | None = None) -> None:
        """find_duplicate(正文哈希) 返回正文相同的已发布版本号或 None；为 None 时不查重。"""
        if not config_hash:
            raise ValueError("config_hash 不能为空")
        self._blobs = blobs
        self._converter = converter
        self._indexer = indexer
        self._config_hash = config_hash
        self._chunking = chunking
        self._find_duplicate = find_duplicate

    def ingest(self, req: IngestRequest) -> IngestOutcome:
        """处理一份原件。

        1. version_id、source_key、filename 为空 → ValueError（不发出任何调用）。
        2. extension = req.extension，为 None 时取 extension_of(filename)。ocr = extension ∈ OCR_EXTENSIONS。
           （格式识别由调用方用 effective_extension 完成；识别不出时调用方不应调用本方法。）
        3. 调用 converter.convert(source_key, filename=with_extension(filename, extension), config_hash=, ocr=)。
           UnsupportedFormatError → 返回 UNSUPPORTED（detail 为异常信息）；其他异常原样抛出。
        4. 从 BlobStore 读取 structure_key，按 UTF-8 解析 JSON；不是 JSON 对象 → PermanentError。
        5. check_conversion(原始 doc, extension=extension)。不通过 → 返回 QC_BLOCKED，不切块、不发布。
        6. fixed, report = restructure(doc)；drafts = chunk_document(fixed, chunking)。没有分块 → 返回 NO_CHUNKS。
        7. hash = content_hash(drafts)；period = document_period(filename, req.folders,
           "\n".join([req.title] + _head_lines(fixed)), "\n".join(d.text for d in drafts))。
        8. 构造时给出的 find_duplicate 不为 None 且 find_duplicate(hash) 返回版本号 → 返回 DUPLICATE（detail “与 <版本号> 正文相同”，不发布）。
        9. chunks = to_chunks(…, title=req.title)；indexer.publish(version_id, chunks, group_tags)，返回 PUBLISHED。发布异常原样抛出。
        各结果都带上已得到的 extension、restructure、content_hash、period（未得到的为 None）。
        """
        if not req.version_id or not req.source_key or not req.filename:
            raise ValueError("version_id、source_key 和 filename 不能为空")
        extension = req.extension or extension_of(req.filename)
        ocr = extension in OCR_EXTENSIONS
        try:
            conversion = self._converter.convert(
                req.source_key, filename=with_extension(req.filename, extension),
                config_hash=self._config_hash, ocr=ocr,
            )
        except UnsupportedFormatError as exc:
            return IngestOutcome(req.version_id, IngestStatus.UNSUPPORTED, ocr,
                                 None, None, (), None, str(exc), extension)
        raw = self._blobs.get(conversion.structure_key)
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise PermanentError("转换结构不是 UTF-8 JSON 对象") from exc
        if not isinstance(doc, dict):
            raise PermanentError("转换结构不是 JSON 对象")
        qc = check_conversion(doc, extension=extension)
        if not qc.passed:
            detail = ", ".join(finding.code.value for finding in qc.findings
                               if finding.severity == Severity.BLOCKING)
            return IngestOutcome(req.version_id, IngestStatus.QC_BLOCKED, ocr,
                                 conversion, qc, (), None, detail, extension)
        fixed, report = restructure(doc)
        drafts = chunk_document(fixed, self._chunking)
        if not drafts:
            return IngestOutcome(req.version_id, IngestStatus.NO_CHUNKS, ocr,
                                 conversion, qc, (), None, "没有可发布的分块", extension, report)
        digest = content_hash(drafts)
        period = document_period(req.filename, req.folders, "\n".join([req.title, *_head_lines(fixed)]),
                                 "\n".join(draft.text for draft in drafts))
        duplicate = self._find_duplicate(digest) if self._find_duplicate is not None else None
        if duplicate is not None:
            return IngestOutcome(req.version_id, IngestStatus.DUPLICATE, ocr, conversion, qc, (), None,
                                 f"与 {duplicate} 正文相同", extension, report, digest, period, duplicate)
        chunks = to_chunks(req.version_id, drafts, title=req.title)
        receipt = self._indexer.publish(req.version_id, chunks, list(req.group_tags))
        return IngestOutcome(req.version_id, IngestStatus.PUBLISHED, ocr,
                             conversion, qc, chunks, receipt, "", extension, report, digest, period)
