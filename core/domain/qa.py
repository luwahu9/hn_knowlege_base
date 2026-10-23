"""问答的提示词构造与答案校验（纯逻辑）。规格：docs/spec-13-qa.md

模型只能根据检索到的资料作答，并且每条引用都必须能在对应资料中逐字找到（引文校验见 quote_check）。
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.domain.quote_check import locate_quote

SYSTEM_PROMPT = (
    "你是企业知识库助手“小安”。只根据用户给出的资料回答问题，不得使用资料以外的知识，不得编造。\n"
    "资料按 [编号] 给出。输出要求：\n"
    "1. answer：简洁直接地回答，给出结论和关键数字；\n"
    "2. citations：支撑答案的引用列表，每项给出资料编号 ref 和从该资料中逐字摘抄的原文 quote"
    "（6 到 80 个字，必须与资料原文完全一致，不得改写、不得拼接）；\n"
    "3. 资料不足以回答时，insufficient 为 true，answer 说明缺少什么信息，citations 可以为空；否则 insufficient 为 false。\n"
    "ref 必须是整数（资料编号，不加引号）。只输出一个 JSON 对象，不要输出其他内容，格式示例：\n"
    '{"answer": "……", "insufficient": false, "citations": [{"ref": 1, "quote": "从资料 1 中逐字摘抄的原文"}]}'
)

_REF_STRING = re.compile(r"[1-9][0-9]{0,3}")


def normalize_ref(value: Any) -> int | None:
    """把模型给出的资料编号规范为整数；无法接受时返回 None。

    接受：int（不接受 bool）；或完全由 1～4 位十进制数字组成、首位不为 0 的 str（如 "1"、"12"）。
    其余一律不接受：前后空白、正负号、前导零、小数、科学计数法、全角或中文数字、空串、float、None 等。
    编号范围（1 ≤ ref ≤ 资料数）由调用方另行检查。
    """
    if type(value) is int:
        return value
    if isinstance(value, str) and _REF_STRING.fullmatch(value):
        return int(value)
    return None

ANSWER_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "insufficient": {"type": "boolean"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"ref": {"type": "integer"}, "quote": {"type": "string"}},
                "required": ["ref", "quote"],
            },
        },
    },
    "required": ["answer", "insufficient", "citations"],
}


@dataclass(frozen=True)
class Passage:
    ref: int                     # 资料编号，从 1 开始连续
    source: str                  # 来源（文档标题）
    section: str | None
    page_start: int | None
    page_end: int | None
    text: str                    # 资料正文（不含面包屑行）


class AnswerStatus(str, Enum):
    ANSWERED = "answered"            # 有答案且至少一条引用通过校验
    UNGROUNDED = "ungrounded"        # 有答案但没有任何引用通过校验
    INSUFFICIENT = "insufficient"    # 模型声明资料不足
    INVALID_OUTPUT = "invalid_output"  # 模型输出无法解析为符合约定的 JSON


@dataclass(frozen=True)
class Citation:
    ref: int
    quote: str                   # 模型给出的引文（原样）
    start: int                   # 在对应 Passage.text 中的位置 [start, end)
    end: int


@dataclass(frozen=True)
class ParsedAnswer:
    status: AnswerStatus
    answer: str
    citations: tuple[Citation, ...]
    dropped: int                 # 被丢弃的引用条数（格式不对、编号越界、引文找不到、重复）


def strip_breadcrumb(text: str) -> str:
    """去掉 render_for_index 加的面包屑行：text 以 "【" 开头且第一行以 "】" 结尾时，去掉第一行；否则原样返回。"""
    first, _, rest = text.partition("\n")
    return rest if first.startswith("【") and first.endswith("】") else text


def build_messages(question: str, passages: Sequence[Passage]) -> list[dict[str, str]]:
    """构造对话消息：[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": ...}]。

    user 内容格式（逐字）：
        资料：
        [1] 来源：<source>；章节：<section 或 "无">；页码：<页码>
        <text>
        （空行）
        [2] ……
        （空行）
        问题：<question>
    页码：page_start 为 None → "无"；起止相同或 page_end 为 None → "<起>"；否则 "<起>-<止>"。
    资料之间、最后一条资料与“问题：”之间各空一行。question 为空白 → ValueError；passages 为空 → ValueError；
    passages 的 ref 必须依次为 1, 2, 3…… → 否则 ValueError。
    """
    question = question.strip()
    if not question or not passages or any(p.ref != i for i, p in enumerate(passages, 1)):
        raise ValueError("问题和资料不能为空，资料编号须从 1 连续递增")
    blocks: list[str] = []
    for p in passages:
        if p.page_start is None:
            page = "无"
        elif p.page_end is None or p.page_start == p.page_end:
            page = str(p.page_start)
        else:
            page = f"{p.page_start}-{p.page_end}"
        blocks.append(f"[{p.ref}] 来源：{p.source}；章节：{p.section or '无'}；页码：{page}\n{p.text}")
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "资料：\n" + "\n\n".join(blocks) + f"\n\n问题：{question}"}]


_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def extract_json(text: str) -> Any | None:
    """从模型文本中取出 JSON：先删除所有 <think>…</think> 块并去首尾空白，再去掉首尾的 ``` 或 ```json 围栏；
    先尝试整体解析，失败则取第一个 "{" 到最后一个 "}" 之间的子串解析；都失败返回 None。"""
    cleaned = _FENCE.sub("", _THINK.sub("", text).strip())
    try:
        return json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end >= start:
            try:
                return json.loads(cleaned[start:end + 1])
            except ValueError:
                pass
    return None


def parse_answer(text: str, parsed: Any | None, passages: Sequence[Passage]) -> ParsedAnswer:
    """校验模型输出。

    1. obj = parsed（是 dict 时）否则 extract_json(text)。obj 不是 dict，或 answer 不是字符串，或 insufficient 不是布尔，
       或 citations 不是列表 → INVALID_OUTPUT（answer 为删除 <think> 块并去首尾空白后的 text，citations 为空，dropped=0）。
    2. 逐条检查 citations：元素须是 dict、normalize_ref(ref) 非 None 且 1 ≤ ref ≤ len(passages)、quote 是字符串，
       且 locate_quote(quote, 该资料 text) 非 None（不检查 80 字上限：提示词中的 6～80 字是对模型的要求，
       程序只强制 locate_quote 的 6 字下限，能逐字定位的长引文保留）；(ref, quote) 与之前保留的重复也丢弃。保留的按原顺序组成 Citation，
       其余计入 dropped。
    3. status：insufficient 为 True → INSUFFICIENT；否则 answer 去空白后为空 → INVALID_OUTPUT；
       否则有保留的引用 → ANSWERED，没有 → UNGROUNDED。
       answer 字段一律去首尾空白。
    """
    obj = parsed if isinstance(parsed, dict) else extract_json(text)
    if (not isinstance(obj, dict) or not isinstance(obj.get("answer"), str)
            or not isinstance(obj.get("insufficient"), bool) or not isinstance(obj.get("citations"), list)):
        return ParsedAnswer(AnswerStatus.INVALID_OUTPUT, _THINK.sub("", text).strip(), (), 0)
    citations: list[Citation] = []
    seen: set[tuple[int, str]] = set()
    dropped = 0
    for item in obj["citations"]:
        if isinstance(item, dict):
            ref, quote = normalize_ref(item.get("ref")), item.get("quote")
            if (ref is not None and 1 <= ref <= len(passages) and isinstance(quote, str)
                    and (ref, quote) not in seen):
                match = locate_quote(quote, passages[ref - 1].text)
                if match is not None:
                    citations.append(Citation(ref, quote, match.start, match.end))
                    seen.add((ref, quote))
                    continue
        dropped += 1
    answer = obj["answer"].strip()
    if obj["insufficient"]:
        status = AnswerStatus.INSUFFICIENT
    elif not answer:
        status = AnswerStatus.INVALID_OUTPUT
    else:
        status = AnswerStatus.ANSWERED if citations else AnswerStatus.UNGROUNDED
    return ParsedAnswer(status, answer, tuple(citations), dropped)
