"""转换结构修整（纯逻辑）：在 Docling 结构 JSON 交给切块器之前，剔除目录、按编号补认标题并统一标题层级。
规格：docs/spec-23-real-docs.md

真实 Word 文档的常见问题：目录行带页码混进正文；“一、”“（一）”“1.”等编号标题是手工排版的普通段落，
Docling 不认作标题，导致整篇不分节、几条新闻挤进同一分块。本模块只改节点的 label、level、content_layer，
不增删节点，输入不被修改（返回深拷贝）。
"""
from __future__ import annotations

import copy
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.domain.chunker import normalize_text

# ---------------------------------------------------------------- 编号样式
_CN = "一二三四五六七八九十"
_STYLES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("chapter", re.compile(f"^第[{_CN}百零〇两0-9]+[章编篇部](?!分)")),
    ("section", re.compile(f"^第[{_CN}百零〇两0-9]+节")),
    ("cn", re.compile(f"^[{_CN}]+[、.．](?!是)")),
    ("cn_paren", re.compile(f"^[（(][{_CN}]+[）)](?!是)")),
    ("dec3", re.compile(r"^\d{1,2}\.\d{1,2}\.\d{1,2}(?![\d.])")),
    ("dec2", re.compile(r"^\d{1,2}\.\d{1,2}(?![\d.%])")),
    ("dec1", re.compile(r"^\d{1,2}(?:[、．]|\.(?!\d)|\s+(?=[^\d\s.%年月日]))")),
    ("num_paren", re.compile(r"^[（(]\d{1,2}[）)]")),
    ("article", re.compile(f"^第[{_CN}百零〇两0-9]+条")),
)
STYLE_ORDER: tuple[str, ...] = ("chapter", "section", "cn", "cn_paren", "dec1", "dec2", "dec3", "num_paren", "article")
MAX_HEADING_CHARS = 60
_ENDS_WITH_DATE = re.compile(r"\d{4}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?\s*[)）]?$")
_SENTENCE_END = tuple("。；;，,：:、")
_TOC_TITLES = {"目录", "目次", "目錄"}
_TOC_LINE = re.compile(r"^(?P<title>.*?\S)(?:\s*(?:\.{2,}|…+|·{2,}|-{2,}))?\s*(?P<page>\d{1,3})$")
MAX_TOC_LINE_CHARS = 80
TOC_SCAN_NODES = 40     # 没有“目录”标题时，只在正文前 40 个文字节点里找目录
MIN_TOC_RUN = 3


def heading_style(text: str) -> str | None:
    """编号样式（STYLE_ORDER 中的一个）；不是编号开头返回 None。text 应已规范化。
    规则按 _STYLES 的顺序匹配，取第一个命中的。"一是""（一）是"这类列举不算编号。"""
    return next((style for style, pattern in _STYLES if pattern.match(text)), None)


def is_heading_candidate(text: str) -> bool:
    """普通段落能否补认为标题：有编号样式；长度 2–MAX_HEADING_CHARS；不以 _SENTENCE_END 中的标点结尾；
    不以完整日期结尾（_ENDS_WITH_DATE，常见于参考文献条目）；编号之后还有文字。text 应已规范化。"""
    if not 2 <= len(text) <= MAX_HEADING_CHARS or text.endswith(_SENTENCE_END) or _ENDS_WITH_DATE.search(text):
        return False
    for _, pattern in _STYLES:
        match = pattern.match(text)
        if match is not None:
            return bool(text[match.end():].strip())
    return False


def toc_entry(text: str) -> str | None:
    """目录行（“标题 …… 12”“标题12”）去掉页码与引导符后的标题；不像目录行返回 None。
    要求：长度 ≤ MAX_TOC_LINE_CHARS；以 1–3 位页码结尾（页码前可有空白或引导符 ..../…/··/--）；
    标题 = 页码与引导符之前的部分（去掉末尾空白和引导符），至少 2 个字符；标题不以数字结尾、不以 _SENTENCE_END 中的标点结尾、
    不以“年”“月”“日”“第”“%”结尾（避免把“截至2024年 3”“增长 12%”这类正文片段误当目录行）。
    拆分标题与页码时，标题取能使整行匹配的最短前缀（即页码取尽量长的 1–3 位尾部数字）。text 应已规范化。"""
    if len(text) > MAX_TOC_LINE_CHARS:
        return None
    match = _TOC_LINE.fullmatch(text)
    if match is None:
        return None
    title = re.sub(r"(?:\s|\.{2,}|…+|·{2,}|-{2,})+$", "", match["title"])
    if len(title) < 2 or title[-1].isdigit() or title.endswith((*_SENTENCE_END, "年", "月", "日", "第", "%")):
        return None
    return title


# ---------------------------------------------------------------- 遍历
def _ordered_texts(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """body 中按阅读顺序（先序遍历 body.children）出现的 texts 节点，不含 content_layer 不是 body 的节点；
    引用格式不对或越界的直接跳过（结构错误交给切块器报告）。"""
    result: list[dict[str, Any]] = []
    visited: set[str] = set()
    pending = list(reversed(doc.get("body", {}).get("children", [])))
    while pending:
        reference = pending.pop()
        ref = reference.get("$ref") if isinstance(reference, Mapping) else None
        if not isinstance(ref, str) or ref in visited:
            continue
        visited.add(ref)
        match = re.fullmatch(r"#/([a-z_]+)/(\d+)", ref)
        if match is None:
            continue
        collection = doc.get(match[1], [])
        index = int(match[2])
        if not isinstance(collection, list) or index >= len(collection):
            continue
        node = collection[index]
        if not isinstance(node, dict) or node.get("content_layer", "body") != "body":
            continue
        if match[1] == "texts":
            result.append(node)
        pending.extend(reversed(node.get("children", [])))
    return result


@dataclass(frozen=True)
class RestructureReport:
    toc_nodes: int           # 标为目录（移出正文）的节点数，含“目录”标题本身
    promoted: int            # 由普通段落补认为标题的节点数
    headings: int            # 修整后正文中的标题总数
    styles: tuple[str, ...]  # 文档中出现的编号样式，按 STYLE_ORDER 排列（决定层级）

    def to_json(self) -> dict[str, Any]:
        return {"toc_nodes": self.toc_nodes, "promoted": self.promoted,
                "headings": self.headings, "styles": list(self.styles)}


def restructure(doc: Mapping[str, Any]) -> tuple[dict[str, Any], RestructureReport]:
    """修整 Docling 结构 JSON，返回 (新文档, 报告)。输入不被修改。

    1. 目录：节点的 text 改为空串（原文保留在 orig 中，原本没有 orig 时写入），并加 "haina_toc": True。
       不改 content_layer——Docling 可能把后续正文挂在目录节点之下，切块器跳过非正文节点时会连子节点一起跳过。
       a) 正文中第一个规范化后（去掉全部空白）属于 _TOC_TITLES 的文字节点，以及紧随其后连续的、toc_entry 不为 None 的节点；
          另外，“目录”二字后紧跟页码的节点（如“目录1”“目 录 1”）也视为目录标题本身。
       b) 没有 a) 时：正文前 TOC_SCAN_NODES 个文字节点中，连续至少 MIN_TOC_RUN 个 toc_entry 不为 None 的节点，
          且其中至少一半的标题（toc_entry 的结果）在该段之后的正文中作为某个节点的完整文字出现 → 这一段是目录。
          只取第一段符合条件的。
    2. 补认标题：其余正文节点中，label 为 "text" 或 "paragraph"、规范化文字满足 is_heading_candidate → label 改为 "section_header"。
    3. 定层级。“原有标题”指修整前 label 已是 section_header 的正文节点；styles 为修整后全部标题中出现的编号样式（按 STYLE_ORDER）。
       a) 原有标题的 level（缺失按 1）有至少两个不同值（Docling 已识别出层级）：原有标题保持原 level；
          补认标题的 level = 同样式原有标题中最常见的 level（并列取较小者）；没有同样式原有标题的，
          level = 原有标题的最大 level + 1 + 该样式在“没有同样式原有标题的样式”（按 STYLE_ORDER）中的序号。
       b) 否则（没有原有标题，或原有标题只有一种 level）：没有编号样式的标题 level = 1；有编号样式的标题
          level = 该样式在 styles 中的序号 + 1 + U，U = 1（存在没有编号样式的标题）或 0。
    """
    new = copy.deepcopy(dict(doc))
    nodes = _ordered_texts(new)
    texts = [normalize_text(node.get("text", "")) for node in nodes]
    entries = [toc_entry(text) for text in texts]
    toc: set[int] = set()
    for index, text in enumerate(texts):
        compact = "".join(text.split())
        entry = entries[index]
        if compact in _TOC_TITLES or (entry is not None and "".join(entry.split()) in _TOC_TITLES):
            toc.add(index)
            cursor = index + 1
            while cursor < len(nodes) and entries[cursor] is not None:
                toc.add(cursor)
                cursor += 1
            break
    if not toc:
        index = 0
        while index < min(len(nodes), TOC_SCAN_NODES):
            end = index
            while end < min(len(nodes), TOC_SCAN_NODES) and entries[end] is not None:
                end += 1
            if end - index >= MIN_TOC_RUN:
                later = set(texts[end:])
                matched = sum(entry in later for entry in entries[index:end])
                if matched * 2 >= end - index:
                    toc.update(range(index, end))
                    break
            index = max(end, index + 1)
    for index in toc:
        node = nodes[index]
        node.setdefault("orig", node.get("text", ""))
        node["text"] = ""
        node["haina_toc"] = True

    original: list[dict[str, Any]] = []
    promoted: list[dict[str, Any]] = []
    for index, node in enumerate(nodes):
        if index in toc:
            continue
        if node.get("label") == "section_header":
            original.append(node)
        elif node.get("label") in {"text", "paragraph"} and is_heading_candidate(texts[index]):
            node["label"] = "section_header"
            promoted.append(node)
    headings = original + promoted
    styles_by_id = {id(node): heading_style(normalize_text(node.get("text", ""))) for node in headings}
    styles = tuple(style for style in STYLE_ORDER if style in styles_by_id.values())
    levels = {node.get("level", 1) for node in original}
    if len(levels) >= 2:
        frequencies: dict[str | None, Counter[int]] = {}
        for node in original:
            frequencies.setdefault(styles_by_id[id(node)], Counter())[node.get("level", 1)] += 1
        missing = [style for style in styles if style not in frequencies]
        for node in promoted:
            style = styles_by_id[id(node)]
            if style in frequencies:
                node["level"] = min(frequencies[style], key=lambda level: (-frequencies[style][level], level))
            else:
                node["level"] = max(levels) + 1 + missing.index(style)
    else:
        offset = int(None in styles_by_id.values())
        for node in headings:
            style = styles_by_id[id(node)]
            node["level"] = 1 if style is None else styles.index(style) + 1 + offset
    return new, RestructureReport(len(toc), len(promoted), len(headings), styles)
