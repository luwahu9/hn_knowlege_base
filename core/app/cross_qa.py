"""跨来源问答（批次 18a）：一个问题同时检索文档、邮件与知识事件，合并（可选重排）后作答。规格见 docs/spec-32-cross-qa.md。"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.app.knowledge import KnowledgeQueries
from core.app.mail import MailIndex
from core.app.ports import LlmGateway, LlmUsage, PermanentError, Role, TransientError
from core.domain.cross_qa import (MAX_ENTITIES, NO_HITS_ANSWER, KEYWORD_SCHEMA, build_cross_messages, build_name_index,
                                  document_text, event_text, event_when, keyword_messages, mail_text, match_names,
                                  merge_order, parse_keywords, resolve_names, retrieval_text)
from core.domain.conversation import REWRITE_SCHEMA, HistoryTurn, Rewrite, parse_rewrite, rewrite_messages
from core.domain.mail import MailDoc
from core.domain.mail_qa import excerpt, passage_text, question_keys
from core.domain.mail_query import MailQuery
from core.domain.permissions import FolderScope, Visibility
from core.domain.qa import ANSWER_SCHEMA, Passage, parse_answer

MODES = ("all", "documents", "mails")
DOC_LIMIT = 6               # 文档候选数
MAIL_CANDIDATES = 30        # 邮件相关度检索的候选数（去重前）
MAIL_LIMIT = 6              # 去重后保留的邮件候选数
EVENTS_PER_ENTITY = 4       # 每个实体取的事件数
MAX_EVENTS = 8              # 事件候选总数
TOP_K = 8                   # 交给模型的资料数


@dataclass(frozen=True)
class DocCandidate:
    """一条文档候选（已按当前账号过滤，已回填章节与页码）。period：period_label 的结果。"""
    version_id: str
    chunk_id: str
    title: str
    section: str | None
    page_start: int | None
    page_end: int | None
    period: str
    text: str
    score: float


class DocumentSource(Protocol):
    def search(self, text: str, *, limit: int) -> list[DocCandidate]:
        """按相关度从高到低，最多 limit 条。只含当前账号可读的已发布文档。"""


class NameDirectory:
    """实体词典：names() 返回 [(名称, entity_id)]（KnowledgeQueries.entity_names）。
    index() 在 names() 返回的对象与上次不是同一个时重建（CachedQueries 在知识数据不变时返回同一对象）。"""

    def __init__(self, names: Callable[[], Sequence[tuple[str, str]]]) -> None:
        self._names = names
        self._source: object | None = None
        self._index: dict[str, tuple[str, ...]] = {}

    def index(self) -> dict[str, tuple[str, ...]]:
        names = self._names()
        if names is not self._source:
            self._index, self._source = build_name_index(names), names
        return self._index


@dataclass(frozen=True)
class CrossSource:
    """交给模型的一条资料。kind 为 "document"、"mail" 或 "event"；定位字段按类型填写，其余为 None。
    when：资料的时间文字（文档为时间段，邮件为发送时间，事件为事件时间）。score：重排分数（没有重排时为 None）。"""
    ref: int
    kind: str
    title: str
    when: str
    score: float | None = None
    version_id: str | None = None       # 文档与事件
    chunk_id: str | None = None
    section: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    mail_id: str | None = None          # 邮件
    sender: str | None = None
    folder: str | None = None
    event_id: str | None = None         # 事件


@dataclass(frozen=True)
class CrossCitation:
    ref: int
    quote: str
    source: CrossSource


@dataclass(frozen=True)
class MatchedEntity:
    text: str                           # 问题中的原文（词典匹配）或模型给出的名称
    entity_id: str


@dataclass(frozen=True)
class CrossQaResult:
    question: str
    mode: str
    status: str                         # AnswerStatus 的值，或 "no_hits"
    answer: str
    citations: tuple[CrossCitation, ...]
    sources: tuple[CrossSource, ...]
    dropped_citations: int
    reranked: bool
    entities: tuple[MatchedEntity, ...]
    keywords_from: str                  # "dictionary"、"model" 或 "none"
    candidates: dict[str, int] = field(default_factory=dict)   # {"documents","mails","events"}：去重后的候选数
    usage: LlmUsage | None = None       # 关键词抽取（或改写）与作答的用量之和；没有调用模型时为 None
    model: str | None = None
    standalone: str = ""                # 批次 19：用于检索与作答的完整问题（没有上文或没有改写时等于 question）
    rewritten: bool = False             # 批次 19：standalone 是否由模型改写而来（与 question 不同）


def _add(total: LlmUsage | None, usage: LlmUsage) -> LlmUsage:
    return usage if total is None else LlmUsage(total.prompt_tokens + usage.prompt_tokens,
                                                total.completion_tokens + usage.completion_tokens)


class CrossQaService:
    """ask(question, *, mode, visibility, mail_scope, mail_query)：

    1. question 去首尾空白后为空 → ValueError；mode 不在 MODES → ValueError；mail_query.terms 非空 → ValueError。
       不发出任何调用。
    2. 实体（mode 不是 "mails" 且 knowledge 与 names 都给出时）：
       - match_names(question, names.index())；有匹配 → keywords_from "dictionary"，实体为各匹配的 entity_ids 依次展开去重，
         最多 MAX_ENTITIES 个（text 为匹配的原文片段）；
       - 没有匹配 → 调模型抽取关键词：llm.complete(Role.REWRITE, keyword_messages(question), json_schema=KEYWORD_SCHEMA,
         temperature=0.0, max_tokens=256, caller="assistant.keywords")，parse_keywords；成功 → keywords_from "model"，
         entities 经 resolve_names 对到词典成为实体（整体匹配时 text 为模型给出的名称，否则为名称中匹配的片段），
         topics 用于检索；
         抛 TransientError 或 PermanentError → keywords_from "none"，不中断。
       - mode 为 "mails"，或 knowledge / names 缺省 → 不识别实体，keywords_from "none"，不调用关键词模型。
    3. 检索文本 = question；有模型给出的 topics 时为 question + " " + 用空格连接的 topics。
    4. 候选（各类按自身顺序）：
       - 文档（mode 为 "all" 或 "documents"，且 documents 给出）：documents.search(检索文本, limit=doc_limit)。
       - 邮件（mode 为 "all" 或 "mails"，且 mails 给出）：mails.relevant(检索文本, mail_query, scope=mail_scope,
         limit=mail_candidates)，按 message_id 去重（同 17b），取前 mail_limit 封。
       - 事件（有实体时）：对每个实体 knowledge.entity_detail(id, limit=events_per_entity * 2, scope=visibility)，
         为 None 跳过；事件先取 events 中有日期的部分从新到旧（列表中靠后的更新），再取没有日期的，只要 verdict 为 "pass" 的，
         每个实体最多 events_per_entity 条；按事件 ID 去重，总数最多 max_events。
    5. 没有任何候选 → status "no_hits"，answer 为 NO_HITS_ANSWER，不调用作答模型。
    6. 资料文本：文档 document_text；邮件 mail_text(passage_text(doc, excerpt(doc.body, question_keys(检索文本))))；
       事件 event_text。rerank 为真时 llm.rerank(question, 全部候选的资料文本, caller="assistant.rerank")：成功 → 按分数
       从高到低稳定排序，reranked 为真；抛 PermanentError 或 TransientError（或数量不符）→ 按 merge_order 合并，reranked 为假。
    7. 取前 top_k 条，编号 1..k，llm.complete(Role.QA, build_cross_messages(question, 资料), json_schema=ANSWER_SCHEMA,
       temperature=0.0, max_tokens=max_tokens, caller="assistant.answer")；异常原样抛出。parse_answer 校验引文。
    8. usage 为关键词抽取（如有）与作答的用量之和；model 为作答模型的 model_alias。"""

    def __init__(self, llm: LlmGateway, *, documents: DocumentSource | None = None, mails: MailIndex | None = None,
                 knowledge: KnowledgeQueries | None = None, names: NameDirectory | None = None,
                 doc_limit: int = DOC_LIMIT, mail_candidates: int = MAIL_CANDIDATES, mail_limit: int = MAIL_LIMIT,
                 events_per_entity: int = EVENTS_PER_ENTITY, max_events: int = MAX_EVENTS, top_k: int = TOP_K,
                 rerank: bool = True, max_tokens: int = 1024) -> None:
        if min(doc_limit, mail_candidates, mail_limit, events_per_entity, max_events, top_k, max_tokens) < 1:
            raise ValueError("各项上限必须为正数")
        self._llm, self._documents, self._mails, self._knowledge, self._names = llm, documents, mails, knowledge, names
        self._doc_limit, self._mail_candidates, self._mail_limit = doc_limit, mail_candidates, mail_limit
        self._events_per_entity, self._max_events, self._top_k = events_per_entity, max_events, top_k
        self._rerank, self._max_tokens = rerank, max_tokens

    def ask(self, question: str, *, mode: str = "all", visibility: Visibility | None = None,
            mail_scope: FolderScope | None = None, mail_query: MailQuery = MailQuery(),
            history: Sequence[HistoryTurn] = ()) -> CrossQaResult:
        question = question.strip()
        if not question:
            raise ValueError("问题不能为空")
        if mode not in MODES:
            raise ValueError("问答范围只能是 all、documents、mails")
        if mail_query.terms:
            raise ValueError("问答时不使用关键词条件")
        usage: LlmUsage | None = None
        entities: list[MatchedEntity] = []
        topics: tuple[str, ...] = ()
        names: tuple[str, ...] = ()            # 模型给出的实体名（无论是否在词典中），并入检索文本
        keywords_from = "none"
        standalone, rewritten = question, False
        rewrite: Rewrite | None = None
        if history:                            # 批次 19：有上文时先改写（同时给出实体名与主题词）
            try:
                result = self._llm.complete(Role.REWRITE, rewrite_messages(history, question), json_schema=REWRITE_SCHEMA,
                                            temperature=0.0, max_tokens=512, caller="assistant.rewrite")
            except (TransientError, PermanentError):
                result = None
            if result is not None:
                usage = _add(usage, result.usage)
                rewrite = parse_rewrite(result.text, result.parsed, question)
                if rewrite.valid:
                    standalone, rewritten = rewrite.question, rewrite.question != question
        if mode != "mails" and self._knowledge is not None and self._names is not None:
            index = self._names.index()
            matches = match_names(standalone, index)
            if matches:
                keywords_from = "dictionary"
                for match in matches:
                    entities += [MatchedEntity(match.text, id_) for id_ in match.entity_ids]
            if rewrite is not None and rewrite.valid:
                keywords_from = keywords_from if matches else "model"
                topics, names = rewrite.topics, rewrite.entities
                entities += [MatchedEntity(text, id_) for text, id_ in resolve_names(rewrite.entities, index)]
            elif not matches and not history:
                try:
                    result = self._llm.complete(Role.REWRITE, keyword_messages(question), json_schema=KEYWORD_SCHEMA,
                                                temperature=0.0, max_tokens=256, caller="assistant.keywords")
                except (TransientError, PermanentError):
                    result = None
                if result is not None:
                    keywords_from = "model"
                    usage = _add(usage, result.usage)
                    keywords = parse_keywords(result.text, result.parsed)
                    topics, names = keywords.topics, keywords.entities
                    entities += [MatchedEntity(text, id_) for text, id_ in resolve_names(keywords.entities, index)]
            seen_ids: set[str] = set()
            unique = []
            for entity in entities:
                if entity.entity_id not in seen_ids:
                    seen_ids.add(entity.entity_id)
                    unique.append(entity)
            entities = unique[:MAX_ENTITIES]
        elif rewrite is not None and rewrite.valid:
            topics, names = rewrite.topics, rewrite.entities
        text = retrieval_text(standalone, topics, names)

        documents = (self._documents.search(text, limit=self._doc_limit)
                     if mode in ("all", "documents") and self._documents is not None else [])
        mails: list[MailDoc] = []
        if mode in ("all", "mails") and self._mails is not None:
            seen: set[str] = set()
            for doc in self._mails.relevant(text, mail_query, scope=mail_scope, limit=self._mail_candidates):
                if doc.message_id is not None:
                    if doc.message_id in seen:
                        continue
                    seen.add(doc.message_id)
                mails.append(doc)
            mails = mails[:self._mail_limit]
        events: list[dict[str, Any]] = []
        if entities and self._knowledge is not None:
            seen_events: set[str] = set()
            for entity in entities:
                detail = self._knowledge.entity_detail(entity.entity_id, limit=self._events_per_entity * 2,
                                                       scope=visibility)
                if detail is None:
                    continue
                rows = [row for row in detail["events"] if row.get("start")][::-1] + \
                       [row for row in detail["events"] if not row.get("start")]
                taken = 0
                for row in rows:
                    if row["verdict"] != "pass" or row["id"] in seen_events or taken >= self._events_per_entity:
                        continue
                    seen_events.add(row["id"])
                    events.append(row)
                    taken += 1
            events = events[:self._max_events]
        counts = {"documents": len(documents), "mails": len(mails), "events": len(events)}
        if not (documents or mails or events):
            return CrossQaResult(question, mode, "no_hits", NO_HITS_ANSWER, (), (), 0, False, tuple(entities),
                                 keywords_from, counts, usage, None, standalone, rewritten)

        keys = question_keys(text)
        pool: dict[tuple[str, int], tuple[str, dict[str, Any]]] = {}
        for i, hit in enumerate(documents):
            pool[("document", i)] = (document_text(hit.title, hit.section, hit.page_start, hit.page_end, hit.period,
                                                   hit.text),
                                     dict(kind="document", title=hit.title, when=hit.period, version_id=hit.version_id,
                                          chunk_id=hit.chunk_id, section=hit.section, page_start=hit.page_start,
                                          page_end=hit.page_end))
        for i, doc in enumerate(mails):
            pool[("mail", i)] = (mail_text(passage_text(doc, excerpt(doc.body, keys))),
                                 dict(kind="mail", title=doc.subject or "（无主题）", when=doc.sent_at or "未知",
                                      mail_id=doc.mail_id, sender=doc.sender.addr if doc.sender else None,
                                      folder=doc.folder))
        for i, row in enumerate(events):
            pool[("event", i)] = (event_text(row),
                                  dict(kind="event", title=row["title"], when=event_when(row), version_id=row["version_id"],
                                       chunk_id=row["chunk_id"], section=row.get("section_path"),
                                       page_start=row.get("page_start"), page_end=row.get("page_end"),
                                       event_id=row["id"]))
        order = [key for key in merge_order(len(events), len(documents), len(mails)) if key in pool]
        scores: dict[tuple[str, int], float] = {}
        reranked = False
        if self._rerank:
            natural = list(pool)
            try:
                values = self._llm.rerank(standalone, [pool[key][0] for key in natural], caller="assistant.rerank")
                if len(values) != len(natural):
                    raise PermanentError("重排结果数量不符")
                scores = dict(zip(natural, values))
                order = sorted(natural, key=lambda key: -scores[key])
                reranked = True
            except (PermanentError, TransientError):
                scores = {}
        chosen = order[:self._top_k]
        passages = [Passage(ref, pool[key][1]["title"], None, None, None, pool[key][0])
                    for ref, key in enumerate(chosen, 1)]
        asked = f"{standalone}（用户原话：{question}）" if rewritten else question
        result = self._llm.complete(Role.QA, build_cross_messages(asked, passages), json_schema=ANSWER_SCHEMA,
                                    temperature=0.0, max_tokens=self._max_tokens, caller="assistant.answer")
        usage = _add(usage, result.usage)
        parsed = parse_answer(result.text, result.parsed, passages)
        sources = tuple(CrossSource(ref=ref, score=scores.get(key), **pool[key][1]) for ref, key in enumerate(chosen, 1))
        citations = tuple(CrossCitation(c.ref, c.quote, sources[c.ref - 1]) for c in parsed.citations)
        return CrossQaResult(question, mode, parsed.status.value, parsed.answer, citations, sources, parsed.dropped,
                             reranked, tuple(entities), keywords_from, counts, usage, result.model_alias, standalone,
                             rewritten)
