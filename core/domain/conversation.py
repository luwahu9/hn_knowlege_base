"""小安多轮对话的纯逻辑（批次 19）：问题改写（同时识别实体与主题词）、对话记录的结构、按当前权限遮蔽历史回答。
规格见 docs/spec-35-conversation.md。

- 有上文时，一次模型调用把本轮问题改写成不依赖上文的完整问题，并给出实体名与主题词（替代 18a 的关键词抽取调用）。
- 作答仍只依据本轮检索到的资料：作答提示词里只有改写后的问题（附用户原话），不放上文，避免模型拿上文的回答当依据。
- 对话记录保存每轮的原话、改写后的问题与完整回答；读取时，引用的资料有任何一条已不可读的轮次，答案与资料一律遮蔽。
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.domain.cross_qa import MAX_KEYWORD_CHARS, MAX_KEYWORDS
from core.domain.qa import extract_json

CONTEXT_TURNS = 4               # 改写时带的最近轮数
CONTEXT_ANSWER_CHARS = 300      # 每轮回答在上文中最多保留的字数
MAX_QUESTION_CHARS = 500        # 与接口的问题长度上限一致；改写结果超过时视为改写失败
MAX_TURNS = 200                 # 一个对话最多的轮数
MAX_TITLE_CHARS = 40
HIDDEN_ANSWER = "（这一轮引用的资料中有你当前无权查看的内容，回答已隐藏。）"
_CONVERSATION_ID = re.compile(r"c_[0-9a-f]{32}")

REWRITE_SYSTEM_PROMPT = (
    "你在帮企业知识库助手“小安”理解多轮对话中的最新问题。给出：\n"
    "question——把最新问题改写成不依赖上文、可以单独检索的完整问题：补全代词、省略的主语和对象、"
    "“那……呢”之类的承接；最新问题本身已经完整时原样给出；不要回答问题，不要加入上文中没有的信息；\n"
    "entities——完整问题涉及的具体的人、组织、地点、项目等对象的名称（写出的照原文；代称或描述指向某个对象且你能确定是谁时，"
    "写出它最常用的中文名称，不能确定就不写）；\n"
    "topics——概括问题主题的词或短语。entities、topics 各最多 5 个，没有就给空列表。\n"
    '只输出一个 JSON 对象，例如 {"question": "特朗普9月17日在密歇根州说了什么？", "entities": ["特朗普"], "topics": ["关税"]}'
)

REWRITE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {"question": {"type": "string"},
                   "entities": {"type": "array", "items": {"type": "string"}},
                   "topics": {"type": "array", "items": {"type": "string"}}},
    "required": ["question", "entities", "topics"],
}


@dataclass(frozen=True)
class HistoryTurn:
    """上文中的一轮：question 为当时改写后的完整问题，answer 为当时的回答。"""
    question: str
    answer: str


@dataclass(frozen=True)
class Rewrite:
    question: str               # 改写后的完整问题；改写结果无效时为原问题
    entities: tuple[str, ...]
    topics: tuple[str, ...]
    valid: bool                 # 模型给出了可用的改写


def rewrite_messages(history: Sequence[HistoryTurn], question: str) -> list[dict[str, str]]:
    """[system, user]。user 内容（逐字）："对话上文：\\n" + 各轮 "用户：<问题>\\n小安：<回答前 CONTEXT_ANSWER_CHARS 字>"
    用空行连接 + "\\n\\n最新问题：<question>"。只取最后 CONTEXT_TURNS 轮。history 为空或 question 为空白 → ValueError。"""
    question = question.strip()
    if not history or not question:
        raise ValueError("改写需要上文和问题")
    blocks = [f"用户：{turn.question}\n小安：{turn.answer[:CONTEXT_ANSWER_CHARS]}" for turn in history[-CONTEXT_TURNS:]]
    return [{"role": "system", "content": REWRITE_SYSTEM_PROMPT},
            {"role": "user", "content": "对话上文：\n" + "\n\n".join(blocks) + f"\n\n最新问题：{question}"}]


def parse_rewrite(text: str, parsed: Any | None, question: str) -> Rewrite:
    """obj = parsed（是 dict 时）否则 extract_json(text)。question 字段去首尾空白后非空且不超过 MAX_QUESTION_CHARS → valid，
    否则 question 用原问题（去首尾空白）、valid 为 False。entities、topics 的清理同 cross_qa.parse_keywords
    （字符串、去空白、去空、不超过 MAX_KEYWORD_CHARS、去重、各最多 MAX_KEYWORDS 个）；valid 为 False 时两者为空。"""
    obj = parsed if isinstance(parsed, dict) else extract_json(text)
    original = question.strip()
    value = obj.get("question") if isinstance(obj, dict) else None
    rewritten = value.strip() if isinstance(value, str) else ""
    if not rewritten or len(rewritten) > MAX_QUESTION_CHARS:
        return Rewrite(original, (), (), False)

    def pick(name: str) -> tuple[str, ...]:
        values = obj.get(name)
        if not isinstance(values, list):
            return ()
        cleaned = [v.strip() for v in values if isinstance(v, str)]
        return tuple(dict.fromkeys(v for v in cleaned if v and len(v) <= MAX_KEYWORD_CHARS))[:MAX_KEYWORDS]
    return Rewrite(rewritten, pick("entities"), pick("topics"), True)


# ---------------------------------------------------------------- 对话记录

def new_conversation_id() -> str:
    return "c_" + uuid.uuid4().hex


def valid_conversation_id(value: str) -> bool:
    return isinstance(value, str) and _CONVERSATION_ID.fullmatch(value) is not None


def title_of(question: str) -> str:
    """对话标题：第一轮问题去首尾空白、把连续空白压成一个空格，最多 MAX_TITLE_CHARS 个字符（超出时末尾为 "…"）。"""
    text = " ".join(question.split())
    return text if len(text) <= MAX_TITLE_CHARS else text[:MAX_TITLE_CHARS - 1] + "…"


@dataclass(frozen=True)
class ConversationSummary:
    conversation_id: str
    owner: str
    title: str
    created_at: str
    updated_at: str
    turns: int


@dataclass(frozen=True)
class Conversation:
    summary: ConversationSummary
    turns: tuple[dict[str, Any], ...] = field(default_factory=tuple)   # 每轮的记录（见 turn_record），按顺序


def turn_record(result: Mapping[str, Any], *, at: str) -> dict[str, Any]:
    """一轮的保存形式：CrossQaResult 的字段（asdict）加 "at"。"""
    return {**result, "at": at}


def turn_sources(turn: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """一轮中引用到的全部资料（sources 与 citations 中的 source）。"""
    return list(turn.get("sources") or []) + [c["source"] for c in turn.get("citations") or []]


def visible_turn(turn: Mapping[str, Any], readable: Callable[[Mapping[str, Any]], bool]) -> dict[str, Any]:
    """按当前权限给出一轮的可见形式。readable(source) 判断一条资料（CrossSource 的字典）当前是否可读。
    全部资料都可读 → 原样（复制），加 "hidden": False；
    否则 answer 换成 HIDDEN_ANSWER，citations、sources、entities 清空，dropped_citations 为 0，加 "hidden": True
    （问题原话与改写后的问题保留：它们是用户自己写的或由用户的话改写而来）。"""
    row = dict(turn)
    if all(readable(source) for source in turn_sources(turn)):
        return {**row, "hidden": False}
    row.update(answer=HIDDEN_ANSWER, citations=[], sources=[], entities=[], dropped_citations=0, hidden=True)
    return row


def history_of(turns: Sequence[Mapping[str, Any]]) -> list[HistoryTurn]:
    """改写用的上文：只取未遮蔽（hidden 为假）且回答未被抹去（purged 为假，批次 20b）的轮次的 (standalone, answer)，
    按顺序，最后 CONTEXT_TURNS 轮。"""
    rows = [HistoryTurn(turn.get("standalone") or turn["question"], turn["answer"]) for turn in turns
            if not turn.get("hidden") and not turn.get("purged")]          # 批次 20b：回答已抹去的轮次也不进上文
    return rows[-CONTEXT_TURNS:]
