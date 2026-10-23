"""跨来源问答的纯逻辑（批次 18a）：问题中的实体识别（词典匹配与模型抽取的解析）、三类资料的格式、带时间意识的提示词、
没有重排时的合并顺序。规格见 docs/spec-32-cross-qa.md。

三类资料：文档分块（检索引擎）、邮件（Elasticsearch）、知识事件（已抽取的事件，按问题中识别出的实体取）。
答案与引文校验沿用 core.domain.qa.parse_answer：每条引文必须能在对应资料的文本中逐字找到。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core.domain.mail import fold
from core.domain.qa import Passage, extract_json

MIN_CJK_NAME = 2                # 词典匹配的最短名称：中日韩字符 2 个
MIN_LATIN_NAME = 3              # 纯拉丁字母或数字的名称至少 3 个字符
MAX_NAME_CHARS = 32             # 匹配时考虑的最长名称
MAX_ENTITIES = 5                # 一个问题最多识别的实体数
MAX_KEYWORDS = 5                # 模型抽取的实体与主题词各自的上限
MAX_KEYWORD_CHARS = 40
NO_HITS_ANSWER = "知识库中暂未收录。"
KINDS = ("document", "mail", "event")

CROSS_SYSTEM_PROMPT = (
    "你是企业知识库助手“小安”。只根据用户给出的资料回答问题，不得使用资料以外的知识，不得编造。\n"
    "资料按 [编号] 给出，类型有文档、邮件和知识事件（从文档中抽取的事件，附原文），每条都标有时间。输出要求：\n"
    "1. answer：简洁直接地回答。问题涉及“现在、目前、最新”等时，以时间最新的资料为准；引用数字、状态或安排时，"
    "在答案中注明它所属的时间；不同资料说法不一致时，指出差异及各自的时间，不要只取其一；\n"
    "2. citations：支撑答案的引用列表，每项给出资料编号 ref 和从该资料中逐字摘抄的原文 quote"
    "（6 到 80 个字，必须与资料原文完全一致，不得改写、不得拼接）；\n"
    "3. 资料不足以回答时，insufficient 为 true，answer 说明缺少什么信息，citations 可以为空；否则 insufficient 为 false。\n"
    "ref 必须是整数（资料编号，不加引号）。只输出一个 JSON 对象，不要输出其他内容，格式示例：\n"
    '{"answer": "……", "insufficient": false, "citations": [{"ref": 1, "quote": "从资料 1 中逐字摘抄的原文"}]}'
)

KEYWORD_SYSTEM_PROMPT = (
    "从用户的问题中找出：entities——问题涉及的具体的人、组织、地点、项目等对象的名称：问题里写出的专有名称照原文写；"
    "问题用代称、简称或描述指向某个对象且你能确定是谁时，写出它最常用的中文名称（不能确定就不写）；"
    "topics——概括问题主题的词或短语。各最多 5 个，没有就给空列表。"
    '只输出一个 JSON 对象，例如 {"entities": ["张伟", "星火计划"], "topics": ["预算调整"]}'
)

KEYWORD_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {"entities": {"type": "array", "items": {"type": "string"}},
                   "topics": {"type": "array", "items": {"type": "string"}}},
    "required": ["entities", "topics"],
}


# ---------------------------------------------------------------- 实体词典

def _is_cjk(char: str) -> bool:
    code = ord(char)
    return 0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF or 0x20000 <= code <= 0x323AF


def name_key(name: str) -> str | None:
    """词典键：fold（NFKC 后转小写）并去首尾空白；太短的名称返回 None：
    含中日韩字符时至少 MIN_CJK_NAME 个字符，否则至少 MIN_LATIN_NAME 个字符；超过 MAX_NAME_CHARS 也返回 None。"""
    key = fold(name).strip()
    if not key or len(key) > MAX_NAME_CHARS:
        return None
    minimum = MIN_CJK_NAME if any(_is_cjk(char) for char in key) else MIN_LATIN_NAME
    return key if len(key) >= minimum else None


def build_name_index(names: Iterable[tuple[str, str]]) -> dict[str, tuple[str, ...]]:
    """names 为 (名称, entity_id)。返回 {name_key: 实体 ID 元组（去重，保持首次出现的顺序）}；name_key 为 None 的跳过。"""
    index: dict[str, list[str]] = {}
    for name, entity_id in names:
        key = name_key(name)
        if key is None:
            continue
        ids = index.setdefault(key, [])
        if entity_id not in ids:
            ids.append(entity_id)
    return {key: tuple(ids) for key, ids in index.items()}


@dataclass(frozen=True)
class NameMatch:
    text: str                   # 问题中的原文片段
    start: int
    end: int
    entity_ids: tuple[str, ...]


def _word_char(char: str) -> bool:
    return char.isalnum() and not _is_cjk(char)


def match_names(question: str, index: Mapping[str, tuple[str, ...]], *, limit: int = MAX_ENTITIES) -> list[NameMatch]:
    """在问题中查找词典里的名称：从左到右，每个位置取能匹配的最长片段（不超过 MAX_NAME_CHARS，首尾不是空白），匹配后跳过该片段；
    片段两端若是拉丁字母或数字，则要求它在问题中不与相邻的拉丁字母或数字相连（"abc" 不匹配 "xabcx"）。
    最多 limit 个，按出现顺序。index 的键须为 name_key 的结果。"""
    matches: list[NameMatch] = []
    position = 0
    while position < len(question) and len(matches) < limit:
        found = None
        for length in range(min(MAX_NAME_CHARS, len(question) - position), 0, -1):
            piece = question[position:position + length]
            if piece[0].isspace() or piece[-1].isspace():
                continue
            key = name_key(piece)
            if key is None or key not in index:
                continue
            before = question[position - 1] if position > 0 else ""
            after = question[position + length] if position + length < len(question) else ""
            if (_word_char(piece[0]) and before and _word_char(before)) or (
                    _word_char(piece[-1]) and after and _word_char(after)):
                continue
            found = NameMatch(piece, position, position + length, index[key])
            break
        if found is None:
            position += 1
        else:
            matches.append(found)
            position = found.end
    return matches


@dataclass(frozen=True)
class Keywords:
    entities: tuple[str, ...]
    topics: tuple[str, ...]


def parse_keywords(text: str, parsed: Any | None) -> Keywords:
    """解析模型抽取的关键词：obj = parsed（是 dict 时）否则 extract_json(text)。entities、topics 各取列表中的字符串，
    去首尾空白，丢弃空串与超过 MAX_KEYWORD_CHARS 的，去重，各最多 MAX_KEYWORDS 个。obj 不是 dict 或字段不是列表时为空。"""
    obj = parsed if isinstance(parsed, dict) else extract_json(text)

    def pick(field: str) -> tuple[str, ...]:
        values = obj.get(field) if isinstance(obj, dict) else None
        if not isinstance(values, list):
            return ()
        cleaned = [value.strip() for value in values if isinstance(value, str)]
        return tuple(dict.fromkeys(value for value in cleaned if value and len(value) <= MAX_KEYWORD_CHARS))[:MAX_KEYWORDS]
    return Keywords(pick("entities"), pick("topics"))


def resolve_names(names: Iterable[str], index: Mapping[str, tuple[str, ...]], *,
                  limit: int = MAX_ENTITIES) -> list[tuple[str, str]]:
    """把模型给出的名称对到词典（批次 18a-r2）：返回 [(名称原文或其中匹配的片段, entity_id)]，按名称顺序、实体 ID 去重，
    最多 limit 个。每个名称先整体查 name_key；整体不在词典中时，用 match_names 在名称内部找词典名称
    （例如 "唐纳德·特朗普" 中的 "特朗普"）。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in names:
        key = name_key(name)
        pairs = [(name, id_) for id_ in index[key]] if key is not None and key in index else [
            (match.text, id_) for match in match_names(name, index) for id_ in match.entity_ids]
        for text, id_ in pairs:
            if id_ not in seen and len(out) < limit:
                seen.add(id_)
                out.append((text, id_))
    return out


def retrieval_text(question: str, topics: Iterable[str], names: Iterable[str]) -> str:
    """检索文本（批次 19 起）：question，再依次接上 topics 与 names 中尚未出现在已有文本里的词（用空格连接，去重）。
    names 为模型给出的实体名：问题用代称时，补上名称才能检索到相关的文档与邮件（18a 的遗留问题）。"""
    text = question
    for word in [*topics, *names]:
        if word and word not in text:
            text += " " + word
    return text


def keyword_messages(question: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": KEYWORD_SYSTEM_PROMPT}, {"role": "user", "content": question.strip()}]


# ---------------------------------------------------------------- 资料格式

def pages_label(page_start: int | None, page_end: int | None) -> str:
    if page_start is None:
        return "无"
    if page_end is None or page_end == page_start:
        return str(page_start)
    return f"{page_start}-{page_end}"


def period_label(period: Mapping[str, Any] | None) -> str:
    """文档时间段（doc_period.Period.to_json() 的字典）的文字：year_only → "YYYY年"；起止相同 → "YYYY-MM-DD"；
    否则 "起至止"；None → "未知"。"""
    if not period:
        return "未知"
    if period.get("year_only"):
        return f"{period['start'][:4]}年"
    if period["start"] == period["end"]:
        return period["start"]
    return f"{period['start']}至{period['end']}"


def document_text(title: str, section: str | None, page_start: int | None, page_end: int | None, period: str,
                  body: str) -> str:
    """文档资料（逐字）："类型：文档\\n来源：<title>；章节：<section 或 无>；页码：<页码>\\n时间：<period>\\n正文：\\n<body>"。"""
    return (f"类型：文档\n来源：{title}；章节：{section or '无'}；页码：{pages_label(page_start, page_end)}\n"
            f"时间：{period}\n正文：\n{body}")


def mail_text(mail_passage: str) -> str:
    """邮件资料：在 mail_qa.passage_text 的结果前加一行 "类型：邮件"。"""
    return "类型：邮件\n" + mail_passage


def event_when(row: Mapping[str, Any]) -> str:
    """事件的时间：start 与 end 都有且不同 → "start至end"；只有 start 或相同 → start；没有日期时用 time_expr；都没有 → "未知"。"""
    start, end = row.get("start"), row.get("end")
    if start:
        return f"{start}至{end}" if end and end != start else start
    return row.get("time_expr") or "未知"


def event_text(row: Mapping[str, Any]) -> str:
    """知识事件资料（逐字），row 为 build_timeline 的一项：
        类型：知识事件
        事件：<label>
        时间：<event_when>
        地点：<place 或 无>
        原文：<quote>
        来源：<title>；页码：<页码>"""
    return (f"类型：知识事件\n事件：{row['label']}\n时间：{event_when(row)}\n地点：{row.get('place') or '无'}\n"
            f"原文：{row['quote']}\n来源：{row['title']}；页码：{pages_label(row.get('page_start'), row.get('page_end'))}")


def build_cross_messages(question: str, passages: Sequence[Passage]) -> list[dict[str, str]]:
    """[{"role": "system", "content": CROSS_SYSTEM_PROMPT}, {"role": "user", "content": ...}]。
    user 内容（逐字）："资料：\\n" + 各条 "[<ref>]\\n<text>" 用空行连接 + "\\n\\n问题：<question>"。
    question 为空白、passages 为空或编号不是 1..n → ValueError。"""
    question = question.strip()
    if not question or not passages or any(p.ref != i for i, p in enumerate(passages, 1)):
        raise ValueError("问题和资料不能为空，资料编号须从 1 连续递增")
    blocks = [f"[{p.ref}]\n{p.text}" for p in passages]
    return [{"role": "system", "content": CROSS_SYSTEM_PROMPT},
            {"role": "user", "content": "资料：\n" + "\n\n".join(blocks) + f"\n\n问题：{question}"}]


def merge_order(events: int, documents: int, mails: int, *, lead_events: int = 3) -> list[tuple[str, int]]:
    """没有重排时的合并顺序：先放至多 lead_events 条事件，然后文档与邮件按各自的检索顺序交替（文档在前），
    一方用完后接着放另一方，最后放剩余的事件。返回 [(类型, 该类型内的序号)]。"""
    order: list[tuple[str, int]] = [("event", i) for i in range(min(lead_events, events))]
    for i in range(max(documents, mails)):
        if i < documents:
            order.append(("document", i))
        if i < mails:
            order.append(("mail", i))
    order += [("event", i) for i in range(min(lead_events, events), events)]
    return order
