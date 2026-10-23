"""用例：对一个已发布版本逐分块抽取实体与业务事件。规格：docs/spec-16-extraction.md"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from core.app.ports import Chunk, LlmGateway, Role
from core.domain.entity_relations import RelationRecord
from core.domain.doc_period import Period
from core.domain.entities import EntityType, ResolutionKind, normalize_name, resolve
from core.domain.events import Verdict, check_event, event_fingerprint
from core.domain.extraction import EXTRACTION_SCHEMA, MIN_NAME_CHARS, build_extraction_messages, parse_extraction
from core.domain.knowledge import EntityRecord, EventRecord, MentionRecord, entity_id, entity_number
from core.domain.qa import strip_breadcrumb
from core.domain.quote_check import locate_quote

EXTRACT_CALLER = "kg.extract"


@dataclass(frozen=True)
class ExtractionReport:
    version_id: str
    chunks: int                  # 实际调用模型的分块数（正文为空的分块不计）
    invalid_outputs: int         # parse_extraction 返回 ok=False 的分块数
    dropped_items: int           # 各分块 Extraction.dropped 之和
    events_pass: int             # 以下四项按 EventRecord 计（含 duplicate_of 非空的事件）
    events_review: int
    events_reject: int
    duplicates: int              # duplicate_of 非空的事件数
    mentions: int
    new_entities: int
    prompt_tokens: int
    completion_tokens: int
    model: str | None            # 最后一次调用的 model_alias；没有调用时为 None
    relations: int = 0           # 批次 23b：生成的 RelationRecord 数

    def to_json(self) -> dict[str, Any]:
        """字段名与属性名相同的扁平字典。"""
        return dict(vars(self))


@dataclass(frozen=True)
class DocumentKnowledge:
    version_id: str
    events: tuple[EventRecord, ...]
    mentions: tuple[MentionRecord, ...]
    new_entities: tuple[EntityRecord, ...]      # 本次新建的实体，按创建顺序
    report: ExtractionReport
    relations: tuple[RelationRecord, ...] = ()  # 批次 23b：本版本的实体关系，按分块顺序、分块内按采纳顺序


class _Recording:
    """记下每次模型调用的结果（批次 16b-Q5）。"""

    def __init__(self, inner: LlmGateway) -> None:
        self._inner, self.results = inner, []

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        result = self._inner.complete(*args, **kwargs)
        self.results.append(result)
        return result


class _Replay:
    """按顺序返回记下的结果，不发出任何请求；调用次数与记录不符说明抽取过程不确定 → RuntimeError。"""

    def __init__(self, results: Sequence[Any]) -> None:
        self._results, self._next = list(results), 0

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        if self._next >= len(self._results):
            raise RuntimeError("重放的模型调用次数与记录不符")
        self._next += 1
        return self._results[self._next - 1]


class ExtractionService:
    def __init__(self, llm: LlmGateway, *, max_tokens: int = 2048) -> None:
        self._llm = llm
        self._max_tokens = max_tokens

    def prepare(self, version_id: str, title: str, chunks: Sequence[Chunk], *, anchor: date | None = None,
                period: Period | None = None) -> Callable[[Sequence[EntityRecord]], DocumentKnowledge]:
        """批次 16b-Q5：两段式抽取。先完成全部模型调用（以空实体池按 extract 的规则走一遍，记下每次调用的结果），
        返回 build(entities)：用记下的结果按同样顺序重放 extract(…, entities)，不再调用模型。
        对同样的模型返回，build(entities) 与 extract(…, entities, …) 的结果完全相同（含报告中的计数与用量）。
        模型调用的异常在 prepare 中原样抛出。build 只做纯计算，供 KnowledgeRepo.save_with 在临界区内调用。"""
        recorder = _Recording(self._llm)
        ExtractionService(recorder, max_tokens=self._max_tokens).extract(version_id, title, chunks, (), anchor=anchor,
                                                                          period=period)
        results = list(recorder.results)

        def build(entities: Sequence[EntityRecord]) -> DocumentKnowledge:
            replay = ExtractionService(_Replay(results), max_tokens=self._max_tokens)
            return replay.extract(version_id, title, chunks, entities, anchor=anchor, period=period)
        return build

    def extract(self, version_id: str, title: str, chunks: Sequence[Chunk], entities: Sequence[EntityRecord], *,
                anchor: date | None = None, period: Period | None = None) -> DocumentKnowledge:
        """逐分块抽取。chunks 按给定顺序处理；entities 是知识库中已有的全部实体（只读，不修改）。

        前置检查（在任何模型调用之前）：entities 的 id 必须唯一且符合 ENTITY_ID，否则 ValueError。
        新实体 ID 从 max(已有序号, 0) + 1 起连续分配（entity_id）。

        对每个分块：
        1. body = strip_breadcrumb(chunk.text)；body 去空白后为空 → 跳过（不调用模型、不计入 chunks）。
        2. llm.complete(Role.EXTRACT, build_extraction_messages(title, chunk.section_path, body),
           json_schema=EXTRACTION_SCHEMA, temperature=0.0, max_tokens=max_tokens, caller=EXTRACT_CALLER)。
           模型调用抛出的任何异常原样向上抛出，本次调用不返回任何部分结果。
        3. ex = parse_extraction(result.text, result.parsed, body)。ex.ok 为 False → invalid_outputs += 1，处理下一分块。
        4. 实体提及：对 ex.mentions 依次 link(name, type)，生成 MentionRecord(role="entity", start/end 取自提及)。
        5. 事件：对 ex.events 依次编号（版本内序号 n 从 1 起，每条 ExtractedEvent 都占一个序号），
           check = check_event(draft, source_text=body, anchor=anchor, period=period)，生成 EventRecord：
           - verdict、problems（枚举值）、草稿各字段、subject_type、object_type 原样记录；
           - check.date 有 value → date_start/date_end/precision；有 issue → date_issue；
           - check.quote_match 有值 → quote_start/quote_end；page_start/page_end/section_path 取自分块；
           - verdict 不是 REJECT 时：
             · 参与者：subject_type 非空且 locate_quote(subject, body, min_chars=MIN_NAME_CHARS) 命中，且能关联到已有实体
               （批次 23a-r2：本次调用的缓存中有同类型同规范名的实体，或 resolve 为 MATCHED；不再为参与者新建实体，
               因为实体列表才受“职务、泛称不是实体”等规则约束）→ subject_entity 为该实体，并生成
               MentionRecord(role="subject", start/end 取该命中)；关联不到 → subject_entity 为 None，不生成提及。
               object 同理（role="object"）。先 subject 后 object。
             · fingerprint = event_fingerprint(subject, action, object, date value 或 None)；抛 ValueError 时为 None。
             · fingerprint 非空且本版本前面已有相同指纹的事件 → duplicate_of 为其中第一条的 event_id。
           - verdict 为 REJECT：不对齐参与者，fingerprint 与 duplicate_of 为 None。
        link(name, type, subtype)（宁可漏合并；批次 23a：新建实体时记下 subtype，即实体列表中该提及的子类，事件参与者为 None；
        已有实体的子类不因后来的提及而改变）：
           - 先查本次调用内的缓存，键为 (type, normalize_name(name))，命中则直接返回缓存的实体 ID；
           - 否则 r = resolve(name, type, 已有实体 + 本次已新建实体)：
             MATCHED → r.entity_id；NEW → 新建实体（canonical=name）；
             NEEDS_REVIEW → 新建实体，review_candidates = 各候选的 entity_id（保持 r.candidates 的顺序）；
           - 结果写入缓存。
        report：按上述计数；mentions 为 MentionRecord 总数；token 用量为各次调用之和。
        """
        ids = [entity.id for entity in entities]
        if len(set(ids)) != len(ids):
            raise ValueError("实体 ID 必须唯一")
        next_number = max((entity_number(id_) for id_ in ids), default=0) + 1
        pool = [entity.to_entity() for entity in entities]
        new_entities: list[EntityRecord] = []
        mentions: list[MentionRecord] = []
        events: list[EventRecord] = []
        relations: list[RelationRecord] = []
        cache: dict[tuple[EntityType, str], str] = {}
        fingerprints: dict[str, str] = {}
        calls = invalid = dropped = prompt_tokens = completion_tokens = 0
        model: str | None = None

        def _link(name: str, type_: EntityType, subtype: str | None = None) -> str:
            nonlocal next_number
            key = (type_, normalize_name(name))
            if key in cache:
                return cache[key]
            resolution = resolve(name, type_, pool)
            if resolution.kind is ResolutionKind.MATCHED:
                id_ = resolution.entity_id
            else:
                id_ = entity_id(next_number)
                next_number += 1
                entity = EntityRecord(id_, type_, name,
                                      review_candidates=tuple(candidate.entity_id for candidate in resolution.candidates),
                                      subtype=subtype)
                new_entities.append(entity)
                pool.append(entity.to_entity())
            cache[key] = id_
            return id_

        def _match(name: str, type_: EntityType) -> str | None:
            """批次 23a-r2：事件参与者只关联到已有实体（本次调用中由实体列表建立的，或知识库中完全同名的），不新建实体。"""
            key = (type_, normalize_name(name))
            if key in cache:
                return cache[key]
            resolution = resolve(name, type_, pool)
            if resolution.kind is ResolutionKind.MATCHED:
                cache[key] = resolution.entity_id
                return resolution.entity_id
            return None

        for chunk in chunks:
            body = strip_breadcrumb(chunk.text)
            if not body.strip():
                continue
            result = self._llm.complete(Role.EXTRACT, build_extraction_messages(title, chunk.section_path, body),
                                        json_schema=EXTRACTION_SCHEMA, temperature=0.0,
                                        max_tokens=self._max_tokens, caller=EXTRACT_CALLER)
            calls += 1
            prompt_tokens += result.usage.prompt_tokens
            completion_tokens += result.usage.completion_tokens
            model = result.model_alias
            extraction = parse_extraction(result.text, result.parsed, body)
            if not extraction.ok:
                invalid += 1
                continue
            dropped += extraction.dropped
            for mention in extraction.mentions:
                entity_id_ = _link(mention.name, mention.type, mention.subtype)
                mentions.append(MentionRecord(version_id, chunk.chunk_id, entity_id_,
                                              mention.name, mention.start, mention.end, "entity"))
            for found in extraction.relations:               # 批次 23b：两端都是本分块实体列表中的实体
                relations.append(RelationRecord(
                    f"{version_id}:l{len(relations) + 1:04d}", version_id, chunk.chunk_id, found.kind,
                    cache[(found.source_type, normalize_name(found.source))],
                    cache[(found.target_type, normalize_name(found.target))], found.source, found.target,
                    found.role, found.time_expr, found.quote, found.start, found.end,
                    chunk.page_start, chunk.page_end, chunk.section_path))
            for extracted in extraction.events:
                draft = extracted.draft
                check = check_event(draft, source_text=body, anchor=anchor, period=period)
                id_ = f"{version_id}:e{len(events) + 1:04d}"
                when = check.date.value if check.date else None
                participants: dict[str, str | None] = {"subject": None, "object": None}
                fingerprint = duplicate_of = None
                if check.verdict is not Verdict.REJECT:
                    for role, name, type_ in (("subject", draft.subject, extracted.subject_type),
                                              ("object", draft.object, extracted.object_type)):
                        if name is not None and type_ is not None:
                            match = locate_quote(name, body, min_chars=MIN_NAME_CHARS)
                            linked = _match(name, type_) if match is not None else None
                            if linked is not None:
                                participants[role] = linked
                                mentions.append(MentionRecord(version_id, chunk.chunk_id, linked, name,
                                                              match.start, match.end, role))
                    try:
                        fingerprint = event_fingerprint(draft.subject, draft.action, draft.object, when)
                    except ValueError:
                        fingerprint = None
                    if fingerprint:
                        duplicate_of = fingerprints.get(fingerprint)
                        fingerprints.setdefault(fingerprint, id_)
                events.append(EventRecord(
                    event_id=id_, version_id=version_id, chunk_id=chunk.chunk_id, verdict=check.verdict,
                    problems=tuple(problem.value for problem in check.problems),
                    subject=draft.subject, action=draft.action, object=draft.object,
                    time_expr=draft.time_expr, place=draft.place, quote=draft.quote,
                    subject_type=extracted.subject_type, object_type=extracted.object_type,
                    subject_entity=participants["subject"], object_entity=participants["object"],
                    date_start=when.start if when else None, date_end=when.end if when else None,
                    precision=when.precision if when else None, date_issue=check.date.issue if check.date else None,
                    quote_start=check.quote_match.start if check.quote_match else None,
                    quote_end=check.quote_match.end if check.quote_match else None,
                    page_start=chunk.page_start, page_end=chunk.page_end, section_path=chunk.section_path,
                    fingerprint=fingerprint, duplicate_of=duplicate_of,
                ))
        report = ExtractionReport(
            version_id, calls, invalid, dropped,
            sum(event.verdict is Verdict.PASS for event in events),
            sum(event.verdict is Verdict.REVIEW for event in events),
            sum(event.verdict is Verdict.REJECT for event in events),
            sum(bool(event.duplicate_of) for event in events), len(mentions), len(new_entities),
            prompt_tokens, completion_tokens, model, len(relations),
        )
        return DocumentKnowledge(version_id, tuple(events), tuple(mentions), tuple(new_entities), report,
                                 tuple(relations))
