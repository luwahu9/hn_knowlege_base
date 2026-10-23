"""引文校验（纯逻辑）。规格：docs/spec-04-quote-check.md

模型给出的证据引文必须能在原文中找到，找不到的一律作废。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

MIN_QUOTE_CHARS = 6


@dataclass(frozen=True)
class QuoteMatch:
    start: int        # 在原文 text 中的起点（含）
    end: int          # 终点（不含），text[start:end] 即命中的原文片段
    occurrences: int  # 规范化后该引文在原文中出现的次数（不重叠）


def locate_quote(quote: str, text: str, *, min_chars: int = MIN_QUOTE_CHARS) -> QuoteMatch | None:
    """在 text 中定位 quote。

    规范化规则：逐字符 NFKC（全角转半角等），丢弃所有空白字符（含换行），区分大小写，
    不做任何近似或语义匹配。
    - 规范化后的 quote 长度 < min_chars → None（太短无法证明什么）
    - 找不到 → None
    - 找到 → 返回首次出现的位置（映射回原文下标）和出现次数
    """
    normalized_quote, _ = _normalize_with_offsets(quote)
    if not normalized_quote or len(normalized_quote) < min_chars:
        return None

    normalized_text, offsets = _normalize_with_offsets(text)
    first = normalized_text.find(normalized_quote)
    if first == -1:
        return None

    return QuoteMatch(
        start=offsets[first],
        end=offsets[first + len(normalized_quote) - 1] + 1,
        occurrences=normalized_text.count(normalized_quote),
    )


def _normalize_with_offsets(value: str) -> tuple[str, list[int]]:
    """逐字符规范化并去除空白，记录每个输出字符对应的原文下标。"""
    chars: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(value):
        for normalized in unicodedata.normalize("NFKC", char):
            if not normalized.isspace():
                chars.append(normalized)
                offsets.append(index)
    return "".join(chars), offsets
