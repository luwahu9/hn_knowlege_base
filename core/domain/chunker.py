"""切块器（纯逻辑）。规格：docs/spec-11-chunking-qc.md

输入是 Docling 结构 JSON（已解析成 dict），输出是带页码和章节路径的分块草稿 ChunkDraft。
不做任何 I/O；用例层负责读取 JSON、生成 chunk_id、转换成端口的 Chunk。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

# ---------------------------------------------------------------- 数据类


@dataclass(frozen=True)
class ChunkingConfig:
    max_chars: int = 800            # 正文分块的最大字符数
    table_max_chars: int = 1500     # 表格分块的最大字符数（超过则按行分组）

    def __post_init__(self) -> None:
        """任一上限小于 50 → ValueError。"""
        if self.max_chars < 50 or self.table_max_chars < 50:
            raise ValueError("正文和表格的分块上限均不得小于 50")


@dataclass(frozen=True)
class ChunkDraft:
    index: int                              # 从 0 开始，连续
    text: str
    page_start: int | None                  # 无页码格式（DOCX/HTML/CSV/ODT/EPUB）为 None
    page_end: int | None
    section_path: tuple[str, ...]           # 章节路径，自顶向下；可为空元组
    kind: str                               # "text" 或 "table"
    table_part: tuple[int, int] | None = None   # 表格被拆分时为 (第几部分, 共几部分)，从 1 开始；否则 None


# ---------------------------------------------------------------- 文本规范化

_CJK = "\u2e80-\u2fff\u3001-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef"
_CJK_GAP = re.compile(f"(?<=[{_CJK}])\\s+(?=[{_CJK}])")
_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """规范化一段文本：
    1. 删除两侧都是中日韩字符（含全角标点，见 _CJK 范围）的空白；
    2. 其余连续空白（含换行、全角空格）合并为一个半角空格；
    3. 去掉首尾空白。
    """
    return _WS.sub(" ", _CJK_GAP.sub("", text)).strip()


# ---------------------------------------------------------------- 标题层级

_CN_NUM = "0-9一二三四五六七八九十百千零〇两"
_HEADING_PATTERNS: tuple[tuple[re.Pattern[str], int | None], ...] = (
    (re.compile(f"^第[{_CN_NUM}]+[章编篇部]"), 1),
    (re.compile(f"^第[{_CN_NUM}]+节"), 2),
    (re.compile(f"^第[{_CN_NUM}]+条"), 3),
    (re.compile(r"^[一二三四五六七八九十]+[、.．]"), 1),
    (re.compile(r"^[（(][一二三四五六七八九十]+[）)]"), 2),
    (re.compile(r"^[（(]\d{1,3}[）)]"), 3),
    # 十进制编号：1 / 1. / 1.2 / 1.2.3，后面必须是空白或非数字、非“.”、非“%年月日”的字符
    (re.compile(r"^(\d{1,3}(?:\.\d{1,3})*)\.?(?=\s|[^\d.%年月日])"), None),
)


def numbered_level(text: str) -> int | None:
    """按标题编号推断层级；无法识别返回 None。text 应已规范化。

    第N章/编/篇/部 → 1；第N节 → 2；第N条 → 3；一、 → 1；（一） → 2；（1） → 3；
    十进制编号 → 段数（"1 引言" → 1，"2.1 特征" → 2，"1.2.3 细节" → 3）；编号后没有内容（如 "1.5"、"1.2.3"）→ None。
    按上述顺序匹配，取第一个命中的规则。
    """
    for pattern, level in _HEADING_PATTERNS:
        match = pattern.match(text)
        if match is not None:
            return level if level is not None else len(match.group(1).split("."))
    return None


# ---------------------------------------------------------------- 遍历

_SKIP_TEXT_LABELS = {"page_header", "page_footer"}


def _resolve_ref(doc: Mapping[str, Any], reference: Mapping[str, Any]) -> tuple[str, str, Mapping[str, Any]]:
    """解析允许的节点引用，并拒绝格式错误或越界的引用。"""
    ref = reference.get("$ref") if isinstance(reference, Mapping) else None
    match = re.fullmatch(r"#/(texts|tables|groups|pictures|key_value_items|form_items)/([0-9]+)", ref) \
        if isinstance(ref, str) else None
    if match is None:
        raise ValueError(f"无效的文档引用：{ref!r}")
    collection, index = match.group(1), int(match.group(2))
    nodes = doc.get(collection, [])
    if not isinstance(nodes, list) or index >= len(nodes):
        raise ValueError(f"文档引用越界：{ref}")
    node = nodes[index]
    if not isinstance(node, Mapping):
        raise ValueError(f"引用目标不是节点：{ref}")
    return ref, collection, node


def _page_numbers(node: Mapping[str, Any]) -> set[int]:
    """收集节点给出的页码，不为无页码节点推断页码。"""
    return {int(prov["page_no"]) for prov in node.get("prov", []) if "page_no" in prov}


def _split_paragraph(text: str, limit: int) -> list[str]:
    """先按句末标点贪心分组，超长单句再按字符数硬切。"""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[。！？；!?;])", text):
        if len(sentence) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(sentence[start:start + limit] for start in range(0, len(sentence), limit))
        elif len(current) + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        pieces.append(current)
    return [piece.strip() for piece in pieces if piece.strip()]


def _render_table(doc: Mapping[str, Any], node: Mapping[str, Any]) -> dict[str, Any] | None:
    """只使用起始单元格渲染表格，保留续表判断和按行分块所需的信息。"""
    data = node.get("data", {})
    cells = [(cell, normalize_text(cell.get("text", ""))) for cell in data.get("table_cells", [])]
    if not any(text for _, text in cells):
        return None
    columns = data["num_cols"]
    by_row: dict[int, list[tuple[Mapping[str, Any], str]]] = {}
    for cell, text in cells:
        by_row.setdefault(cell["start_row_offset_idx"], []).append((cell, text))

    captions: list[str] = []
    for reference in node.get("captions", []):
        _, _, caption = _resolve_ref(doc, reference)
        text = normalize_text(caption.get("text", ""))
        if text:
            captions.append(text)
    preamble: list[str] = []
    headers: list[str] = []
    rows: list[tuple[str, set[int]]] = []
    pages = _page_numbers(node)
    phase = "before"
    for row_index in range(data["num_rows"]):
        row = by_row.get(row_index, [])
        if not row:
            continue
        first, first_text = row[0]
        banner = (len(row) == 1 and first["start_col_offset_idx"] == 0
                  and first["end_col_offset_idx"] >= columns)
        if banner:
            line = first_text
            if not line:
                continue
        else:
            slots = [""] * columns
            for cell, text in row:
                slots[cell["start_col_offset_idx"]] = text.replace("|", "\\|")
            if not any(slots):
                continue
            line = "| " + " | ".join(slots) + " |"
        if banner and phase == "before":
            preamble.append(line)
        elif not banner and phase != "body" and all(cell.get("column_header", False) for cell, _ in row):
            headers.append(line)
            phase = "header"
        else:
            rows.append((line, pages))
            phase = "body"

    prefix = captions + preamble + headers
    if headers:
        prefix.append("| " + " | ".join(["---"] * columns) + " |")
    return {"captions": captions, "preamble": preamble, "headers": headers,
            "prefix": prefix, "rows": rows, "pages": set(pages)}


def _table_parts(table: Mapping[str, Any], limit: int) -> list[tuple[str, set[int], tuple[int, int] | None]]:
    """重复前缀，按表体行分组；每组至少保留一行并计算来源页码。"""
    prefix = table["prefix"]
    rows = table["rows"]
    text = "\n".join(prefix + [line for line, _ in rows])
    if len(text) <= limit or not rows:
        return [(text, table["pages"], None)]

    groups: list[list[tuple[str, set[int]]]] = []
    current: list[tuple[str, set[int]]] = []
    prefix_length = len("\n".join(prefix))
    length = prefix_length
    for row in rows:
        added_length = len(row[0]) + (1 if prefix or current else 0)
        if current and length + added_length > limit:
            groups.append(current)
            current = []
            length = prefix_length
            added_length = len(row[0]) + (1 if prefix else 0)
        current.append(row)
        length += added_length
    if current:
        groups.append(current)

    parts: list[tuple[str, set[int], tuple[int, int] | None]] = []
    for index, group in enumerate(groups, 1):
        pages = set().union(*(pages for _, pages in group))
        text = "\n".join(prefix + [line for line, _ in group])
        parts.append((text, pages or table["pages"], (index, len(groups))))
    return parts


# ---------------------------------------------------------------- 主函数


def chunk_document(doc: Mapping[str, Any], config: ChunkingConfig | None = None) -> list[ChunkDraft]:
    """把 Docling 结构 JSON 切成分块草稿。规则见 docs/spec-11-chunking-qc.md。"""
    if not isinstance(doc, Mapping) or not isinstance(doc.get("body"), Mapping):
        raise ValueError("文档及其 body 必须是映射对象")
    children = doc["body"].get("children")
    if not isinstance(children, list):
        raise ValueError("body.children 必须是列表")
    config = config if config is not None else ChunkingConfig()
    headings = [node for node in doc.get("texts", []) if node.get("label") == "section_header"]
    flat = len({node.get("level", 1) for node in headings}) <= 1
    inferred_levels = [level for node in headings
                       if (level := numbered_level(normalize_text(node.get("text", "")))) is not None]
    default_level = min(inferred_levels, default=1)
    caption_refs = {reference.get("$ref") for table in doc.get("tables", [])
                    for reference in table.get("captions", [])}

    drafts: list[ChunkDraft] = []
    sections: list[tuple[int, str]] = []
    paragraphs: list[str] = []
    paragraph_chars = 0
    paragraph_pages: set[int] = set()
    pending_table: dict[str, Any] | None = None

    def _emit(text: str, pages: set[int], kind: str, part: tuple[int, int] | None = None) -> None:
        drafts.append(ChunkDraft(len(drafts), text, min(pages) if pages else None,
                                 max(pages) if pages else None, tuple(title for _, title in sections), kind, part))

    def _flush_text() -> None:
        nonlocal paragraph_chars
        if paragraphs:
            _emit("\n".join(paragraphs), paragraph_pages, "text")
            paragraphs.clear()
            paragraph_pages.clear()
            paragraph_chars = 0

    def _flush_table() -> None:
        nonlocal pending_table
        if pending_table is not None:
            for text, pages, part in _table_parts(pending_table, config.table_max_chars):
                _emit(text, pages, "table", part)
            pending_table = None

    def _add_paragraph(text: str, pages: set[int]) -> None:
        nonlocal paragraph_chars
        _flush_table()
        for piece in _split_paragraph(text, config.max_chars):
            if paragraphs and paragraph_chars + len(paragraphs) + len(piece) > config.max_chars:
                _flush_text()
            paragraphs.append(piece)
            paragraph_chars += len(piece)
            paragraph_pages.update(pages)

    visited: set[str] = set()
    stack = list(reversed(children))
    while stack:
        ref, collection, node = _resolve_ref(doc, stack.pop())
        if ref in visited:
            continue
        visited.add(ref)
        if node.get("content_layer", "body") != "body":
            continue
        if collection == "tables":
            table = _render_table(doc, node)
            if table is None:
                continue
            _flush_text()
            if (pending_table is not None and pending_table["headers"]
                    and table["headers"] == pending_table["headers"]
                    and not table["captions"] and not table["preamble"]):
                pending_table["rows"].extend(table["rows"])
                pending_table["pages"].update(table["pages"])
            else:
                _flush_table()
                pending_table = table
            continue
        if collection in {"pictures", "key_value_items", "form_items"}:
            continue
        label = node.get("label")
        if collection == "groups":
            if label == "sheet":
                _flush_text()
                _flush_table()
                sections.clear()
                name = normalize_text(node.get("name", ""))
                if name:
                    sections.append((0, name))
        else:
            if label in _SKIP_TEXT_LABELS or (label == "caption" and ref in caption_refs):
                continue
            raw_text = node.get("text", "")
            if label == "formula" and not raw_text.strip():
                raw_text = node.get("orig", "")
            text = normalize_text(raw_text)
            if text:
                if label == "section_header":
                    _flush_text()
                    _flush_table()
                    level = (numbered_level(text) or default_level) if flat else node.get("level", 1)
                    while sections and sections[-1][0] >= level:
                        sections.pop()
                    sections.append((level, text))
                else:
                    if label == "list_item":
                        marker = normalize_text(node.get("marker", "")) or "-"
                        text = marker + " " + text
                    _add_paragraph(text, _page_numbers(node))
        stack.extend(reversed(node.get("children", [])))

    _flush_text()
    _flush_table()
    return drafts


# ---------------------------------------------------------------- 索引文本


def render_for_index(draft: ChunkDraft, *, title: str) -> str:
    """生成写入检索引擎的文本：第一行是面包屑【标题 > 章节 > …】，表格分块拆分时加“（续表 i/n）”，
    第二行起是分块正文。title、章节、续表编号三者都为空时不加面包屑行
    （只有续表编号时输出【（续表 i/n）】）。"""
    crumbs = [normalize_text(title), *draft.section_path]
    breadcrumb = " > ".join(crumb for crumb in crumbs if crumb)
    if draft.kind == "table" and draft.table_part is not None:
        part, count = draft.table_part
        breadcrumb += f"（续表 {part}/{count}）"
    return f"【{breadcrumb}】\n{draft.text}" if breadcrumb else draft.text
