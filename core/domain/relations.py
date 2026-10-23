"""文档连线：引用、替代、冲突（纯逻辑）。规格：docs/spec-18-links.md

引用与替代：模型对一个分块的正文输出 {"references": [...]}，本模块解析并校验；标题对齐在读取时用 resolve_title 完成。
冲突：程序用 conflict_candidates 挑出候选事件对，模型只看双方引文作判断，本模块解析并校验。
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.domain.entities import QUOTE_CHARS
from core.domain.events import Verdict
from core.domain.knowledge import EventRecord, event_label
from core.domain.qa import extract_json
from core.domain.quote_check import locate_quote

MAX_REFERENCES = 30        # 每个分块最多采纳的引用条目数，超出部分计入 dropped
MIN_TITLE_CHARS = 2        # 书名在引文中定位、以及对齐时的最少字符数（规范化后）
MIN_REF_QUOTE_CHARS = 6    # 引文在正文中定位的最少字符数
MIN_FUZZY_CHARS = 4        # 包含关系对齐时，双方标题规范化后的最少字符数
MAX_REASON_CHARS = 200     # 冲突理由的最大长度，超出部分截断
MIN_DETAIL_CHARS = 2       # 冲突片段在引文中定位的最少字符数


class RelationKind(str, Enum):
    CITES = "cites"
    SUPERSEDES = "supersedes"
    CONFLICTS = "conflicts"


REFERENCE_LABELS: dict[str, tuple[RelationKind, bool]] = {
    "引用": (RelationKind.CITES, False),
    "替代": (RelationKind.SUPERSEDES, False),
    "被替代": (RelationKind.SUPERSEDES, True),
}

REFERENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["references"],
    "properties": {
        "references": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "relation", "quote"],
                "properties": {
                    "title": {"type": "string"},
                    "relation": {"type": "string", "enum": list(REFERENCE_LABELS)},
                    "quote": {"type": "string"},
                },
            },
        },
    },
}

REFERENCE_SYSTEM_PROMPT = (
    "你是海纳知识库的文档关系抽取助手。只根据用户给出的正文，找出正文明确提到的其他文件（制度、办法、合同、通知、报告等）。\n"
    "每一项给出：\n"
    "   title：被提到的文件名称，必须从正文逐字摘抄，可以带书名号；\n"
    "   relation：只能是 引用、替代、被替代 之一。引用＝本文引用、依据、参照该文件；"
    "替代＝本文取代、废止、修订了该文件；被替代＝该文件取代、废止了本文；\n"
    "   quote：从正文逐字摘抄的一句原文，必须包含 title。\n"
    "“该办法”“上述通知”这类没有写出文件名称的指称不要输出。没有可抽取的内容时返回空数组。\n"
    "只输出一个 JSON 对象，不要输出其他文字。格式示例：\n"
    '{"references": [{"title": "采购管理办法", "relation": "引用", "quote": "本合同依据《采购管理办法》执行"}]}'
)

JUDGE_LABELS = ("冲突", "一致", "无关", "不确定")

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["relation", "reason", "detail_a", "detail_b"],
    "properties": {
        "relation": {"type": "string", "enum": list(JUDGE_LABELS)},
        "reason": {"type": "string"},
        "detail_a": {"type": "string"},
        "detail_b": {"type": "string"},
    },
}

JUDGE_SYSTEM_PROMPT = (
    "你是海纳知识库的文档核对助手。用户给出来自两份不同文档的两段原文（甲、乙），请判断它们对同一事项的说法是否互相矛盾。\n"
    "relation 只能是：冲突（同一事项，说法不能同时成立，例如日期、金额、数量、结论不同）、"
    "一致（同一事项，说法相符）、无关（不是同一事项）、不确定。\n"
    "不同时间发生的不同事件不算冲突。判断为冲突时，reason 用一句话说明矛盾点，"
    "detail_a、detail_b 分别从甲、乙原文逐字摘抄产生矛盾的片段；其他情况 detail_a、detail_b 填空字符串。\n"
    "只输出一个 JSON 对象，不要输出其他文字。格式示例：\n"
    '{"relation": "冲突", "reason": "两份文档对签约日期的说法不同", '
    '"detail_a": "2026年4月18日", "detail_b": "2026年4月20日"}'
)


# ---------------------------------------------------------------- 引用与替代：提示词与解析
@dataclass(frozen=True)
class Reference:
    title: str                 # 去掉首尾书名号、引号和空白后的书名
    kind: RelationKind         # CITES 或 SUPERSEDES
    reverse: bool              # True：title 对应的文件是新文，本文是旧文（仅“被替代”）
    quote: str                 # 去首尾空白后的引文
    start: int                 # 引文在 source_text 中首次命中的位置（locate_quote 的 start/end）
    end: int


@dataclass(frozen=True)
class References:
    ok: bool                   # False：输出不是约定的 JSON（此时 items 为空、dropped 为 0）
    items: tuple[Reference, ...]
    dropped: int               # 被丢弃的条目数（格式不对、关系无效、引文或书名定位不到、超出上限）


def build_reference_messages(title: str, section: str | None, text: str) -> list[dict[str, str]]:
    """返回 [system, user] 两条消息。system 为 REFERENCE_SYSTEM_PROMPT；user 内容严格为：

        "文档：{title.strip()}\\n章节：{section.strip() 或 "无"}\\n正文：\\n{text}"

    section 为 None 或去空白后为空时写“无”。text 原样写入，不做 strip。
    title 或 text 去空白后为空 → ValueError。
    """
    if not title.strip() or not text.strip():
        raise ValueError("标题和正文不能为空")
    return [{"role": "system", "content": REFERENCE_SYSTEM_PROMPT},
            {"role": "user", "content": f"文档：{title.strip()}\n章节：{(section or '').strip() or '无'}\n正文：\n{text}"}]


def clean_title(title: str) -> str:
    """反复去掉首尾的空白、引号和书名号（QUOTE_CHARS 中的字符），直到不再变化。不做 NFKC，不改变中间内容。"""
    while True:
        cleaned = title.strip().strip(QUOTE_CHARS).strip()
        if cleaned == title:
            return cleaned
        title = cleaned


def parse_references(raw: str, parsed: Any, source_text: str) -> References:
    """解析模型输出。

    1. 取对象：parsed 是 dict 时用 parsed，否则用 extract_json(raw)。结果必须是 dict，且 "references" 键存在、值是 list；
       否则返回 References(False, (), 0)。
    2. 按顺序处理前 MAX_REFERENCES 条，其余每条 dropped += 1。一条有效需同时满足：是 dict；title、relation、quote 都是 str；
       relation 去首尾空白后在 REFERENCE_LABELS 中；title 经 clean_title 后非空；
       locate_quote(quote, source_text, min_chars=MIN_REF_QUOTE_CHARS) 不为 None；
       locate_quote(clean_title(title), quote, min_chars=MIN_TITLE_CHARS) 不为 None（书名必须出现在引文里）。无效 → dropped += 1。
    3. 有效但与已采纳的某条 (kind, reverse, normalize_title(书名)) 相同 → 跳过，不计 dropped。
       采纳的条目生成 Reference(clean_title(title), kind, reverse, quote.strip(), 引文命中的 start, end)。
    """
    data = parsed if isinstance(parsed, dict) else extract_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("references"), list):
        return References(False, (), 0)
    items: list[Reference] = []
    seen: set[tuple[RelationKind, bool, str]] = set()
    dropped = max(0, len(data["references"]) - MAX_REFERENCES)
    for row in data["references"][:MAX_REFERENCES]:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ("title", "relation", "quote")):
            dropped += 1
            continue
        title = clean_title(row["title"])
        relation = REFERENCE_LABELS.get(row["relation"].strip())
        match = locate_quote(row["quote"], source_text, min_chars=MIN_REF_QUOTE_CHARS)
        if not title or relation is None or match is None or locate_quote(title, row["quote"], min_chars=MIN_TITLE_CHARS) is None:
            dropped += 1
            continue
        kind, reverse = relation
        key = (kind, reverse, normalize_title(title))
        if key in seen:
            continue
        seen.add(key)
        items.append(Reference(title, kind, reverse, row["quote"].strip(), match.start, match.end))
    return References(True, tuple(items), dropped)


# ---------------------------------------------------------------- 标题对齐
_EXTENSION = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|md|txt|html?)$")


def normalize_title(title: str) -> str:
    """标题规范化（用于比较的键）：NFKC → casefold → 去掉全部空白 → 去掉末尾一个文件扩展名
    （.pdf .doc .docx .xls .xlsx .ppt .pptx .md .txt .htm .html，已在 casefold 之后匹配）→ 去掉所有 QUOTE_CHARS 字符。结果可能为空串。"""
    key = "".join(unicodedata.normalize("NFKC", title).casefold().split())
    key = _EXTENSION.sub("", key)
    return "".join(char for char in key if char not in QUOTE_CHARS)


class TitleMatch(str, Enum):
    MATCHED = "matched"
    CANDIDATES = "candidates"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class TitleResolution:
    kind: TitleMatch
    version_ids: tuple[str, ...]     # MATCHED：恰好一个；CANDIDATES：按版本号升序；UNRESOLVED：空


def resolve_title(title: str, documents: Mapping[str, str], *, exclude: str | None = None) -> TitleResolution:
    """把引用中的书名对齐到文档（宁可漏连）。documents 为 {version_id: 标题}（调用方只传已发布的文档）。

    key = normalize_title(title)；len(key) < MIN_TITLE_CHARS → UNRESOLVED。
    候选池：documents 中版本号不等于 exclude 的文档。
    1. 规范化标题与 key 相同的文档恰好 1 个 → MATCHED；多于 1 个 → CANDIDATES（全部，按版本号升序）。
    2. 没有相同的：len(key) ≥ MIN_FUZZY_CHARS 的前提下，规范化标题长度 ≥ MIN_FUZZY_CHARS 且与 key 互相包含
       （key 在其中，或其在 key 中）的文档 → CANDIDATES（哪怕只有一个，按版本号升序）。
    3. 其余 → UNRESOLVED。
    """
    key = normalize_title(title)
    if len(key) < MIN_TITLE_CHARS:
        return TitleResolution(TitleMatch.UNRESOLVED, ())
    pool = {version: normalize_title(value) for version, value in documents.items() if version != exclude}
    exact = tuple(sorted(version for version, value in pool.items() if value == key))
    if exact:
        return TitleResolution(TitleMatch.MATCHED if len(exact) == 1 else TitleMatch.CANDIDATES, exact)
    fuzzy = tuple(sorted(version for version, value in pool.items()
                         if len(key) >= MIN_FUZZY_CHARS and len(value) >= MIN_FUZZY_CHARS
                         and (key in value or value in key)))
    return TitleResolution(TitleMatch.CANDIDATES if fuzzy else TitleMatch.UNRESOLVED, fuzzy)


# ---------------------------------------------------------------- 引用记录与视图
@dataclass(frozen=True)
class ReferenceRecord:
    ref_id: str                  # f"{version_id}:r{序号:04d}"，序号在一个版本内从 1 起（按分块顺序、分块内按采纳顺序）
    version_id: str              # 提出引用的文档（证据所在文档）
    chunk_id: str
    kind: RelationKind           # CITES 或 SUPERSEDES
    reverse: bool
    title: str                   # 模型给出的书名（clean_title 之后）
    quote: str
    quote_start: int             # 引文在分块正文（去面包屑后）中的位置
    quote_end: int
    page_start: int | None
    page_end: int | None
    section_path: str | None

    def to_json(self) -> dict[str, Any]:
        """字段名与属性名相同的扁平字典；kind 写枚举值。"""
        return {**vars(self), "kind": self.kind.value}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ReferenceRecord:
        """to_json 的逆运算；缺少键或枚举值无效 → KeyError / ValueError。"""
        values = {key: data[key] for key in cls.__dataclass_fields__}
        values["kind"] = RelationKind(values["kind"])
        return cls(**values)


def build_links(records: Iterable[ReferenceRecord], documents: Mapping[str, str]) -> dict[str, list[dict[str, Any]]]:
    """引用视图：{"links": [...], "pending": [...], "unresolved": [...]}。documents 为 {version_id: 标题}，只含已发布的文档。

    只处理 version_id 在 documents 中的记录，按 ref_id 升序；对每条记录 resolve_title(title, documents, exclude=version_id)：
    - MATCHED → links；CANDIDATES → pending；UNRESOLVED → unresolved。
    每一行都有：id(ref_id)、kind(枚举值)、reverse、version_id、title(书名)、chunk_id、quote、page_start、page_end、section_path。
    links 另加 source、target（reverse 为 False：source=version_id、target=对齐到的版本；True：两者互换）、source_title、target_title。
    pending 另加 candidates：[{"version_id","title"}…]，按版本号升序。
    """
    result: dict[str, list[dict[str, Any]]] = {"links": [], "pending": [], "unresolved": []}
    for record in sorted(records, key=lambda r: r.ref_id):
        if record.version_id not in documents:
            continue
        row = {"id": record.ref_id, "kind": record.kind.value, "reverse": record.reverse,
               "version_id": record.version_id, "title": record.title, "chunk_id": record.chunk_id,
               "quote": record.quote, "page_start": record.page_start, "page_end": record.page_end,
               "section_path": record.section_path}
        match = resolve_title(record.title, documents, exclude=record.version_id)
        if match.kind is TitleMatch.MATCHED:
            source, target = record.version_id, match.version_ids[0]
            if record.reverse:
                source, target = target, source
            row.update(source=source, target=target, source_title=documents[source], target_title=documents[target])
            result["links"].append(row)
        elif match.kind is TitleMatch.CANDIDATES:
            row["candidates"] = [{"version_id": version, "title": documents[version]} for version in match.version_ids]
            result["pending"].append(row)
        else:
            result["unresolved"].append(row)
    return result


# ---------------------------------------------------------------- 冲突：候选配对
def pair_id(first: str, second: str) -> str:
    """一对事件的 ID：两个 event_id 按字典序升序，用 "|" 连接。两者相同 → ValueError。"""
    if first == second:
        raise ValueError("事件不能与自身配对")
    return "|".join(sorted((first, second)))


def action_key(action: str) -> str:
    """冲突配对用的规范化行为：NFKC、去掉全部空白、casefold。"""
    return "".join(unicodedata.normalize("NFKC", action).split()).casefold()


def conflict_candidates(events: Iterable[EventRecord], *, judged: Collection[str] = (),
                        limit: int = 100) -> list[tuple[EventRecord, EventRecord]]:
    """挑出需要让模型判断的事件对。limit < 1 → ValueError。

    合格事件：verdict 为 PASS、duplicate_of 为空、subject_entity 非空、行为规范化（NFKC、去全部空白、casefold）后非空。
    同一 (subject_entity, 规范化行为) 组内，任意两个 version_id 不同的事件成对；指纹都非空且相同的不成对；
    pair_id 在 judged 中的不成对。每对内 event_id 较小者在前。结果按 (前者 event_id, 后者 event_id) 升序，最多 limit 对。
    """
    if limit < 1:
        raise ValueError("配对上限必须为正整数")
    groups: dict[tuple[str, str], list[EventRecord]] = {}
    for event in events:
        action = action_key(event.action)
        if event.verdict is Verdict.PASS and not event.duplicate_of and event.subject_entity and action:
            groups.setdefault((event.subject_entity, action), []).append(event)
    excluded = set(judged)
    pairs: list[tuple[EventRecord, EventRecord]] = []
    for group in groups.values():
        group.sort(key=lambda event: event.event_id)
        for index, first in enumerate(group):
            for second in group[index + 1:]:
                if first.version_id == second.version_id:
                    continue
                if first.fingerprint and second.fingerprint and first.fingerprint == second.fingerprint:
                    continue
                if pair_id(first.event_id, second.event_id) not in excluded:
                    pairs.append((first, second))
    return sorted(pairs, key=lambda pair: (pair[0].event_id, pair[1].event_id))[:limit]


# ---------------------------------------------------------------- 冲突：判断
class ConflictVerdict(str, Enum):
    CONFLICT = "conflict"        # 模型判为冲突，且理由和双方片段都经校验
    REVIEW = "review"            # 模型判为冲突但证据不成立，或模型回答不确定，或输出格式异常
    CONSISTENT = "consistent"
    UNRELATED = "unrelated"


@dataclass(frozen=True)
class Judgement:
    ok: bool                              # False：输出不是约定的 JSON
    verdict: ConflictVerdict | None       # ok 为 False 时为 None
    reason: str
    detail_a: str
    detail_b: str


def build_judge_messages(title_a: str, quote_a: str, title_b: str, quote_b: str) -> list[dict[str, str]]:
    """返回 [system, user] 两条消息。system 为 JUDGE_SYSTEM_PROMPT；user 内容严格为：

        "文档甲：{title_a.strip()}\\n原文甲：{quote_a.strip()}\\n\\n文档乙：{title_b.strip()}\\n原文乙：{quote_b.strip()}"

    四个参数任何一个去空白后为空 → ValueError。
    """
    if any(not value.strip() for value in (title_a, quote_a, title_b, quote_b)):
        raise ValueError("文档标题和引文不能为空")
    return [{"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": f"文档甲：{title_a.strip()}\n原文甲：{quote_a.strip()}\n\n文档乙：{title_b.strip()}\n原文乙：{quote_b.strip()}"}]


def parse_judgement(raw: str, parsed: Any, quote_a: str, quote_b: str) -> Judgement:
    """解析模型输出。

    1. 取对象：parsed 是 dict 时用 parsed，否则用 extract_json(raw)。必须是 dict，且 "relation" 是 str、
       去首尾空白后在 JUDGE_LABELS 中；否则返回 Judgement(False, None, "", "", "")。
    2. reason：是 str 则去首尾空白并截取前 MAX_REASON_CHARS 个字符，否则空串。
       detail_a / detail_b：是 str 且去空白后 locate_quote(detail, quote_a / quote_b, min_chars=MIN_DETAIL_CHARS) 命中 → 去空白后的原值，
       否则空串。
    3. 一致 → CONSISTENT，无关 → UNRELATED（reason、detail 照第 2 步，不另行清空）；不确定 → REVIEW；
       冲突 → reason 非空且 detail_a、detail_b 都非空时 CONFLICT，否则 REVIEW。
    """
    data = parsed if isinstance(parsed, dict) else extract_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("relation"), str) or data["relation"].strip() not in JUDGE_LABELS:
        return Judgement(False, None, "", "", "")
    reason = data.get("reason")
    reason = reason.strip()[:MAX_REASON_CHARS] if isinstance(reason, str) else ""
    details = []
    for key, quote in (("detail_a", quote_a), ("detail_b", quote_b)):
        value = data.get(key)
        details.append(value.strip() if isinstance(value, str)
                       and locate_quote(value.strip(), quote, min_chars=MIN_DETAIL_CHARS) is not None else "")
    relation = data["relation"].strip()
    verdict = {"一致": ConflictVerdict.CONSISTENT, "无关": ConflictVerdict.UNRELATED,
               "不确定": ConflictVerdict.REVIEW, "冲突": ConflictVerdict.CONFLICT}[relation]
    if verdict is ConflictVerdict.CONFLICT and not (reason and all(details)):
        verdict = ConflictVerdict.REVIEW
    return Judgement(True, verdict, reason, *details)


@dataclass(frozen=True)
class ConflictRecord:
    pair_id: str                 # pair_id(event_a, event_b)
    event_a: str                 # event_id 较小者
    event_b: str
    version_a: str
    version_b: str
    verdict: ConflictVerdict
    reason: str
    detail_a: str
    detail_b: str

    def to_json(self) -> dict[str, Any]:
        """字段名与属性名相同的扁平字典；verdict 写枚举值。"""
        return {**vars(self), "verdict": self.verdict.value}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ConflictRecord:
        """to_json 的逆运算；缺少键或枚举值无效 → KeyError / ValueError。"""
        values = {key: data[key] for key in cls.__dataclass_fields__}
        values["verdict"] = ConflictVerdict(values["verdict"])
        return cls(**values)


def build_conflicts(records: Iterable[ConflictRecord], events: Mapping[str, EventRecord], titles: Mapping[str, str],
                    documents: Collection[str]) -> list[dict[str, Any]]:
    """冲突视图。documents 为已发布的 version_id 集合；titles 为 {version_id: 标题}（取不到时用 version_id）。

    只取 verdict 为 CONFLICT 或 REVIEW、event_a 和 event_b 都在 events 中、且 version_a 和 version_b 都在 documents 中的记录，
    按 pair_id 升序。每行：{"id": pair_id, "verdict", "reason", "detail_a", "detail_b", "version_a", "version_b",
    "a": 事件面, "b": 事件面}；事件面 = {"event_id","version_id","title","label"(event_label),"quote","chunk_id",
    "page_start","page_end","section_path"}。
    """
    rows: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda row: row.pair_id):
        if (record.verdict not in (ConflictVerdict.CONFLICT, ConflictVerdict.REVIEW)
                or record.event_a not in events or record.event_b not in events
                or record.version_a not in documents or record.version_b not in documents):
            continue
        faces = []
        for id_ in (record.event_a, record.event_b):
            event = events[id_]
            faces.append({"event_id": event.event_id, "version_id": event.version_id,
                          "title": titles.get(event.version_id, event.version_id), "label": event_label(event),
                          "quote": event.quote, "chunk_id": event.chunk_id, "page_start": event.page_start,
                          "page_end": event.page_end, "section_path": event.section_path})
        rows.append({"id": record.pair_id, "verdict": record.verdict.value, "reason": record.reason,
                     "detail_a": record.detail_a, "detail_b": record.detail_b,
                     "version_a": record.version_a, "version_b": record.version_b, "a": faces[0], "b": faces[1]})
    return rows


# ---------------------------------------------------------------- 图谱边
def document_edges(links: Iterable[Mapping[str, Any]], conflicts: Iterable[Mapping[str, Any]], *,
                   include_review: bool = True) -> list[dict[str, Any]]:
    """文档之间的图谱边，供 build_graph 的 doc_edges 使用。links 为 build_links()["links"]，conflicts 为 build_conflicts() 的结果。

    - 引用/替代：按 (kind, source, target) 聚合，边 {"id": f"r:{kind}:{source}:{target}", "source", "target"（版本号）,
      "kind": 枚举值, "weight": 行数, "needs_review": False}。
    - 冲突：include_review 为 False 时忽略 verdict 为 review 的行；其余按 (min(version_a, version_b), max(...)) 聚合，
      边 {"id": f"x:{低}:{高}", "source": 低, "target": 高, "kind": "conflicts", "weight": 行数,
      "needs_review": 该组没有 verdict 为 conflict 的行}。
    结果按 id 升序。
    """
    counts = Counter((row["kind"], row["source"], row["target"]) for row in links)
    edges = [{"id": f"r:{kind}:{source}:{target}", "source": source, "target": target,
              "kind": kind, "weight": weight, "needs_review": False}
             for (kind, source, target), weight in counts.items()]
    groups: dict[tuple[str, str], list[str]] = {}
    for row in conflicts:
        if not include_review and row["verdict"] == "review":
            continue
        source, target = sorted((row["version_a"], row["version_b"]))
        groups.setdefault((source, target), []).append(row["verdict"])
    for (source, target), verdicts in groups.items():
        edges.append({"id": f"x:{source}:{target}", "source": source, "target": target,
                      "kind": "conflicts", "weight": len(verdicts), "needs_review": "conflict" not in verdicts})
    return sorted(edges, key=lambda edge: edge["id"])
