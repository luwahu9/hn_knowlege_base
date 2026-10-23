"""实体与业务事件抽取：提示词、输出解析（纯逻辑）。规格：docs/spec-16-extraction.md

模型对一个分块的正文输出 {"entities": [...], "events": [...]}。本模块只负责构造提示词和把输出解析成结构化草稿；
事件是否成立由 core.domain.events.check_event 判定，实体对齐由 core.domain.entities.resolve 判定（均在用例层调用）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.domain.entities import EntityType, normalize_name
from core.domain.entity_relations import PROMPT_RULES, RELATION_LABELS, ExtractedRelation, parse_relations
from core.domain.events import EventDraft
from core.domain.qa import extract_json
from core.domain.quote_check import locate_quote
from core.domain.taxonomy import TYPE_LABELS, TYPE_NAMES, normalize_subtype, prompt_types
MAX_ENTITIES = 50          # 每个分块最多采纳的实体条目数，超出部分计入 dropped
MAX_EVENTS = 20            # 每个分块最多采纳的事件条目数，超出部分计入 dropped
MIN_NAME_CHARS = 2         # 实体名称在正文中定位时的最少字符数（规范化后）

_TYPE_ENUM = list(TYPE_NAMES.values())                 # 批次 23a：9 种
_LABEL_ENUM = _TYPE_ENUM + ["其他"]

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entities", "events", "relations"],
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "type", "subtype"],
                "properties": {"name": {"type": "string"}, "type": {"type": "string", "enum": _TYPE_ENUM},
                               "subtype": {"type": "string"}},
            },
        },
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["subject", "subject_type", "action", "object", "object_type", "time", "place", "quote"],
                "properties": {
                    "subject": {"type": "string"},
                    "subject_type": {"type": "string", "enum": _LABEL_ENUM},
                    "action": {"type": "string"},
                    "object": {"type": "string"},
                    "object_type": {"type": "string", "enum": _LABEL_ENUM},
                    "time": {"type": "string"},
                    "place": {"type": "string"},
                    "quote": {"type": "string"},
                },
            },
        },
        "relations": {                                       # 批次 23b
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "source", "target", "role", "time", "quote"],
                "properties": {
                    "type": {"type": "string", "enum": list(RELATION_LABELS)},
                    "source": {"type": "string"}, "target": {"type": "string"}, "role": {"type": "string"},
                    "time": {"type": "string"}, "quote": {"type": "string"},
                },
            },
        },
    },
}

SYSTEM_PROMPT = (
    "你是海纳知识库的信息抽取助手，为小安整理知识图谱和时间轴。只根据用户给出的正文抽取，不得补充正文以外的信息。\n"
    "1. entities：正文中出现的有名称的实体。name 必须从正文逐字摘抄，用原文中最完整的写法；"
    "type 是下列类型之一，subtype 是该类型冒号后的子类之一（判断不了填空字符串 \"\"）：\n"
    + prompt_types() + "\n"
    "   职务、头衔、岗位（如“部长”“首席财务官”）不是实体；泛称（如“甲方”“公司”“员工”“当地合作方”）不是实体；"
    "命名事件与项目必须有专门的名称（如“第三届中新数字经济论坛”“青鸾计划”），一般的行为不是；"
    "法律与条约必须是有正式名称的文件（如“两国边界划定协定”“采购管理办法”），"
    "没有专名的合同、协议、文件类别（如“设备运输合同”“采购合同”）不是实体；"
    "装备与设施要写出具体型号或名称（如“F-35战斗机”），泛泛的“战斗机”不算。\n"
    "2. events：正文所述的现实业务事件，例如签订、发布、任命、访问、交付、付款、召开会议、发生事故、发动袭击；"
    "文档本身的结构（目录、章节、表头）不是事件。每个事件给出：\n"
    "   subject 主体、action 行为（可简短归纳，如“签订”）、object 对象、time 时间、place 地点，"
    "除 action 外都必须从正文逐字摘抄，没有就填空字符串 \"\"；\n"
    "   subject_type、object_type 取上面的类型之一，或“其他”；\n"
    "   quote 是从正文逐字摘抄的一句原文，必须同时包含 subject、object、time、place 中所有非空的字段。\n"
    + PROMPT_RULES + "\n"
    "时间照抄原文写法（如“2026年4月18日”“上月”），不要换算。没有可抽取的内容时返回空数组。\n"
    "只输出一个 JSON 对象，不要输出其他文字。格式示例：\n"
    '{"entities": [{"name": "华东物流有限公司", "type": "组织", "subtype": "企业"}, '
    '{"name": "韩晓岚", "type": "人物", "subtype": ""}], '
    '"events": [{"subject": "华东物流有限公司", "subject_type": "组织", "action": "签订", "object": "采购合同", '
    '"object_type": "其他", "time": "2026年4月18日", "place": "", '
    '"quote": "华东物流有限公司于2026年4月18日签订采购合同"}], '
    '"relations": [{"type": "分属", "source": "韩晓岚", "target": "华东物流有限公司", "role": "首席财务官", '
    '"time": "2025年起", "quote": "韩晓岚2025年起任华东物流有限公司首席财务官"}]}'
)


@dataclass(frozen=True)
class EntityMention:
    name: str                  # 模型给出的名称，去首尾空白
    type: EntityType
    start: int                 # 在 source_text 中首次命中的位置（locate_quote 的 start/end）
    end: int
    subtype: str | None = None  # 批次 23a：taxonomy.normalize_subtype 的结果


@dataclass(frozen=True)
class ExtractedEvent:
    draft: EventDraft
    subject_type: EntityType | None    # 标签不在 TYPE_LABELS 中（含“其他”、空串、None）→ None
    object_type: EntityType | None


@dataclass(frozen=True)
class Extraction:
    ok: bool                                # False：输出不是约定的 JSON（此时其余字段为空、dropped 为 0）
    mentions: tuple[EntityMention, ...]
    events: tuple[ExtractedEvent, ...]
    dropped: int                            # 被丢弃的条目数（格式不对、名称定位不到、超出上限）
    relations: tuple[ExtractedRelation, ...] = ()   # 批次 23b


PAIRED_MARKS = ("《》", "〈〉", "“”", "‘’", "「」", "『』", '""', "''")


def strip_marks(name: str) -> str:
    """批次 23a-r2：实体名称去首尾空白，再反复去掉外层成对的书名号、引号（PAIRED_MARKS）及其内侧空白。
    例：" 《两国边界划定协定》 " → "两国边界划定协定"；"“天穹-3”防空系统" 不变（不是外层成对）。"""
    name = name.strip()
    while len(name) >= 2 and any(name[0] == pair[0] and name[-1] == pair[1] for pair in PAIRED_MARKS):
        name = name[1:-1].strip()
    return name


def build_extraction_messages(title: str, section: str | None, text: str) -> list[dict[str, str]]:
    """返回 [system, user] 两条消息。system 为 SYSTEM_PROMPT；user 内容严格为：

        "文档：{title.strip()}\\n章节：{section.strip() 或 "无"}\\n正文：\\n{text}"

    section 为 None 或去空白后为空时写“无”。text 原样写入，不做 strip。
    title 或 text 去空白后为空 → ValueError。
    """
    if not title.strip() or not text.strip():
        raise ValueError("标题和正文不能为空")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"文档：{title.strip()}\n章节：{(section or '').strip() or '无'}\n正文：\n{text}"},
    ]


def parse_extraction(raw: str, parsed: Any, source_text: str) -> Extraction:
    """解析模型输出。

    1. 取对象：parsed 是 dict 时用 parsed，否则用 extract_json(raw)。结果必须是 dict，且 "entities" 与 "events"
       两个键都存在、值都是 list；否则返回 Extraction(False, (), (), 0)。
    2. entities：按顺序处理前 MAX_ENTITIES 条，其余每条 dropped += 1。一条有效需同时满足：是 dict；name 与 type 都是 str；
       name 去首尾空白后非空；type（去首尾空白）在 TYPE_LABELS 中；locate_quote(name, source_text, min_chars=MIN_NAME_CHARS)
       不为 None。无效 → dropped += 1。有效但与已采纳的某条 (type, normalize_name(name)) 相同 → 跳过，不计 dropped。
       采纳的条目生成 EntityMention(name, 类型, match.start, match.end, 子类)。
       批次 23a-r2：name 先经 strip_marks（去掉外层成对的书名号、引号），再定位与去重。
       批次 23a：子类 = normalize_subtype(类型, item.get("subtype"))；缺失、不是字符串或不在该类型的子类中 → None，
       实体照样采纳（子类错误不算无效条目）。
    3. events：按顺序处理前 MAX_EVENTS 条，其余每条 dropped += 1。一条有效需满足：是 dict；subject、action、quote 都是 str；
       object、time、place、subject_type、object_type 各自缺失、为 None 或为 str。否则 dropped += 1。
       有效条目生成 ExtractedEvent：
       - EventDraft(subject=subject.strip(), action=action.strip(), object=…, time_expr=time, place=…, quote=quote.strip())，
         其中 object、time、place 去首尾空白后为空或为 None → None；
       - subject_type / object_type：去首尾空白后在 TYPE_LABELS 中则取对应类型，否则 None。
       事件条目在这里不做内容校验（主体为空、引文找不到等由 check_event 判定），也不去重。
    4. relations（批次 23b）：parse_relations(data.get("relations"), {规范名: 类型}（已采纳的实体）, source_text)；
       没有这个键或不是列表 → 空（不算无效输出）；其丢弃数并入 dropped。
    """
    data = parsed if isinstance(parsed, dict) else extract_json(raw)
    if not isinstance(data, dict) or not all(isinstance(data.get(key), list) for key in ("entities", "events")):
        return Extraction(False, (), (), 0)
    mentions: list[EntityMention] = []
    events: list[ExtractedEvent] = []
    seen: set[tuple[EntityType, str]] = set()
    dropped = max(0, len(data["entities"]) - MAX_ENTITIES) + max(0, len(data["events"]) - MAX_EVENTS)
    for item in data["entities"][:MAX_ENTITIES]:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("name", "type")):
            dropped += 1
            continue
        name, type_ = strip_marks(item["name"]), TYPE_LABELS.get(item["type"].strip())
        match = locate_quote(name, source_text, min_chars=MIN_NAME_CHARS) if name and type_ is not None else None
        if match is None:
            dropped += 1
            continue
        key = (type_, normalize_name(name))
        if key not in seen:
            seen.add(key)
            mentions.append(EntityMention(name, type_, match.start, match.end,
                                          normalize_subtype(type_, item.get("subtype"))))
    for item in data["events"][:MAX_EVENTS]:
        if (not isinstance(item, dict)
                or not all(isinstance(item.get(key), str) for key in ("subject", "action", "quote"))
                or not all(item.get(key) is None or isinstance(item[key], str)
                           for key in ("object", "time", "place", "subject_type", "object_type"))):
            dropped += 1
            continue
        events.append(ExtractedEvent(
            EventDraft(subject=item["subject"].strip(), action=item["action"].strip(),
                       object=(item.get("object") or "").strip() or None,
                       time_expr=(item.get("time") or "").strip() or None,
                       place=(item.get("place") or "").strip() or None, quote=item["quote"].strip()),
            TYPE_LABELS.get((item.get("subject_type") or "").strip()),
            TYPE_LABELS.get((item.get("object_type") or "").strip()),
        ))
    listed = {normalize_name(mention.name): mention.type for mention in mentions}
    relations, relation_dropped = parse_relations(data.get("relations"), listed, source_text)
    return Extraction(True, tuple(mentions), tuple(events), dropped + relation_dropped, tuple(relations))
