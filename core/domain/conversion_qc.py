"""转换质检（纯逻辑）。规格：docs/spec-11-chunking-qc.md

识别“转换服务报告成功，但内容有问题”的情况。QcReport.passed 就是放行规则中 rules_passed 的来源。
只看 Docling 结构 JSON，不看 Markdown。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class Severity(str, Enum):
    BLOCKING = "blocking"    # 阻断：不得放行
    WARNING = "warning"      # 警告：放行，但写入审核提示


class QcCode(str, Enum):
    """质检项。枚举定义顺序即报告中的输出顺序。"""

    EMPTY_CONTENT = "empty_content"
    CONTROL_SEQUENCE_RESIDUE = "control_sequence_residue"
    MOJIBAKE = "mojibake"
    PAGE_TEXT_MISSING = "page_text_missing"
    TABLE_MISSING = "table_missing"
    EMPTY_TABLE = "empty_table"
    LOW_TEXT_DENSITY = "low_text_density"


@dataclass(frozen=True)
class QcConfig:
    min_total_chars: int = 20            # 有效字符少于此值 → EMPTY_CONTENT
    control_word_min_hits: int = 10      # RTF 控制字命中数达到此值 → CONTROL_SEQUENCE_RESIDUE
    mojibake_block_ratio: float = 0.01   # 乱码字符占比达到此值且数量 ≥ mojibake_block_min → 阻断
    mojibake_block_min: int = 5
    min_chars_per_page: int = 30         # 有页文档平均每页有效字符少于此值 → LOW_TEXT_DENSITY


@dataclass(frozen=True)
class QcFinding:
    code: QcCode
    severity: Severity
    detail: str                          # 中文说明，给审核人看；测试不比较其内容
    pages: tuple[int, ...] = ()          # 相关页码，升序去重；无页码时为空


@dataclass(frozen=True)
class QcReport:
    findings: tuple[QcFinding, ...]

    @property
    def passed(self) -> bool:
        """没有任何 BLOCKING 项即为通过。"""
        return not any(finding.severity == Severity.BLOCKING for finding in self.findings)


TABLE_FORMATS = frozenset({"xlsx", "xls", "csv"})

_SKIP_TEXT_LABELS = {"page_header", "page_footer"}

_RTF_CONTROL = re.compile(
    r"\\(?:rtf\d|ansi(?:cpg\d+)?|deff\d+|fonttbl|colortbl|pard?|plain|ql|qr|qc|qj|intbl|cell|row|trowd"
    r"|f\d+|fs\d+|sa\d+|sb\d+|li\d+|ri\d+|fi-?\d+|outlinelevel\d+|cf\d+|u-?\d+|'[0-9a-fA-F]{2})(?![a-zA-Z])"
)
_MOJIBAKE = re.compile(
    "\ufffd|锟斤拷|[\ue000-\uf8ff]|[\u0080-\u009f]|[\u00c2-\u00ef][\u0080-\u00bf]"
)


def check_conversion(
    doc: Mapping[str, Any], *, extension: str, config: QcConfig | None = None
) -> QcReport:
    """检查一份 Docling 结构 JSON。extension 是原件扩展名（不区分大小写，可带或不带前导“.”）。

    语料：content_layer 为 body（缺省视为 body）的 texts（排除 page_header / page_footer 标签；
    formula 文本为空时用 orig）+ content_layer 为 body 的 tables 的全部单元格文本。
    “有效字符”= 非空白字符。各规则独立判断，详见规格。
    """
    if not isinstance(doc, Mapping):
        raise ValueError("文档必须是映射对象")
    config = config if config is not None else QcConfig()
    corpus: list[str] = []
    covered_pages: set[int] = set()
    empty_table_pages: set[int] = set()
    empty_tables = 0
    nonempty_tables = 0

    for item in doc.get("texts", []):
        if (item.get("content_layer", "body") != "body"
                or item.get("label") in _SKIP_TEXT_LABELS):
            continue
        text = item.get("text", "")
        if item.get("label") == "formula" and not text.strip():
            text = item.get("orig", "")
        corpus.append(text)
        if text.strip():
            covered_pages.update(_page_numbers(item))

    for table in doc.get("tables", []):
        if table.get("content_layer", "body") != "body":
            continue
        texts = [cell.get("text", "") for cell in table.get("data", {}).get("table_cells", [])]
        corpus.extend(texts)
        if any(text.strip() for text in texts):
            nonempty_tables += 1
            covered_pages.update(_page_numbers(table))
        else:
            empty_tables += 1
            empty_table_pages.update(_page_numbers(table))

    text = "\n".join(corpus)
    total = sum(not char.isspace() for char in text)
    pages = {int(page) for page in doc.get("pages", {})}
    findings: list[QcFinding] = []
    if total < config.min_total_chars:
        findings.append(QcFinding(QcCode.EMPTY_CONTENT, Severity.BLOCKING,
                                  f"有效字符共 {total} 个，少于要求的 {config.min_total_chars} 个。"))

    control_hits = sum(1 for _ in _RTF_CONTROL.finditer(text))
    if control_hits >= config.control_word_min_hits:
        findings.append(QcFinding(QcCode.CONTROL_SEQUENCE_RESIDUE, Severity.BLOCKING,
                                  f"发现 {control_hits} 处 RTF 控制字残留。"))

    bad = sum(len(match.group()) for match in _MOJIBAKE.finditer(text))
    if bad > 0:
        blocking = bad >= config.mojibake_block_min and bad >= config.mojibake_block_ratio * total
        findings.append(QcFinding(QcCode.MOJIBAKE, Severity.BLOCKING if blocking else Severity.WARNING,
                                  f"发现 {bad} 个乱码字符，有效字符共 {total} 个。"))

    missing_pages = tuple(sorted(pages - covered_pages))
    if missing_pages:
        blocking = len(pages) >= 2 and len(missing_pages) * 2 > len(pages)
        findings.append(QcFinding(QcCode.PAGE_TEXT_MISSING,
                                  Severity.BLOCKING if blocking else Severity.WARNING,
                                  f"共 {len(pages)} 页，其中 {len(missing_pages)} 页没有正文或非空表格。",
                                  missing_pages))

    if extension.lower().removeprefix(".") in TABLE_FORMATS and nonempty_tables == 0:
        findings.append(QcFinding(QcCode.TABLE_MISSING, Severity.BLOCKING,
                                  "表格类原件没有转换出任何非空正文表格。"))
    if empty_tables:
        findings.append(QcFinding(QcCode.EMPTY_TABLE, Severity.WARNING,
                                  f"发现 {empty_tables} 张空正文表格。", tuple(sorted(empty_table_pages))))
    if pages and total / len(pages) < config.min_chars_per_page:
        findings.append(QcFinding(QcCode.LOW_TEXT_DENSITY, Severity.WARNING,
                                  f"有效字符共 {total} 个，共 {len(pages)} 页，"
                                  f"平均每页少于 {config.min_chars_per_page} 个字符。"))
    return QcReport(tuple(findings))


def _page_numbers(node: Mapping[str, Any]) -> set[int]:
    """收集节点所覆盖的页码，不从页序或其他节点推断。"""
    return {int(prov["page_no"]) for prov in node.get("prov", []) if "page_no" in prov}
