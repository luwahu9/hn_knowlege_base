"""问答用例：检索 → 构造资料 → 模型作答 → 引文校验。规格：docs/spec-13-qa.md"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.app.ports import Hit, Indexer, LlmGateway, LlmUsage, Role
from core.app.retrieval import ChunkLookup, resolve_hits
from core.domain.qa import (
    ANSWER_SCHEMA,
    Passage,
    build_messages,
    parse_answer,
    strip_breadcrumb,
)

NO_HITS_ANSWER = "知识库中暂未收录。"


@dataclass(frozen=True)
class SourceRef:
    ref: int
    version_id: str
    chunk_id: str
    source: str
    section: str | None
    page_start: int | None
    page_end: int | None
    score: float


@dataclass(frozen=True)
class QaCitation:
    ref: int
    quote: str
    version_id: str
    chunk_id: str
    source: str
    section: str | None
    page_start: int | None
    page_end: int | None


@dataclass(frozen=True)
class QaResult:
    question: str
    status: str                      # AnswerStatus 的值，或 "no_hits"
    answer: str
    citations: tuple[QaCitation, ...]
    sources: tuple[SourceRef, ...]   # 提供给模型的全部资料（按 ref 顺序）
    dropped_citations: int
    usage: LlmUsage | None           # 未调用模型时为 None
    model: str | None


class QaService:
    def __init__(self, indexer: Indexer, llm: LlmGateway, catalog: ChunkLookup,
                 titles: Mapping[str, str], *, top_k: int = 6, max_tokens: int = 1024) -> None:
        """titles：version_id → 文档标题；找不到时用 version_id 作为来源名。top_k < 1 或 max_tokens < 1 → ValueError。"""
        if top_k < 1 or max_tokens < 1:
            raise ValueError("top_k 和 max_tokens 必须为正数")
        self._indexer = indexer
        self._llm = llm
        self._catalog = catalog
        self._titles = titles
        self._top_k = top_k
        self._max_tokens = max_tokens

    def ask(self, question: str, *, group_tags: Sequence[str]) -> QaResult:
        """回答一个问题。

        1. question 去首尾空白后为空 → ValueError（不发出任何调用）。
        2. hits = resolve_hits(indexer.search(question, group_tags=group_tags, top_k=top_k), catalog)。
        3. 没有命中 → status "no_hits"，answer = NO_HITS_ANSWER，不调用模型。
        4. 按命中顺序编号 1..n 构造 Passage：source = titles.get(version_id, version_id)，section/page 取自命中，
           text = strip_breadcrumb(hit.text)。
        5. llm.complete(Role.QA, build_messages(question, passages), json_schema=ANSWER_SCHEMA, temperature=0.0,
           max_tokens=max_tokens, caller="qa.answer")；异常原样抛出。
        6. parse_answer(result.text, result.parsed, passages)，把每条 Citation 映射回对应命中的版本、分块、来源、章节、页码。
        7. 返回 QaResult：status 为 AnswerStatus 的值，sources 为全部资料，usage 与 model 取自模型结果（model_alias）。
        """
        question = question.strip()
        if not question:
            raise ValueError("问题不能为空")
        hits = resolve_hits(self._indexer.search(question, group_tags=group_tags, top_k=self._top_k), self._catalog)
        if not hits:
            return QaResult(question, "no_hits", NO_HITS_ANSWER, (), (), 0, None, None)
        passages = [Passage(i, self._titles.get(hit.version_id, hit.version_id), hit.section_path,
                            hit.page_start, hit.page_end, strip_breadcrumb(hit.text))
                    for i, hit in enumerate(hits, 1)]
        result = self._llm.complete(Role.QA, build_messages(question, passages), json_schema=ANSWER_SCHEMA,
                                    temperature=0.0, max_tokens=self._max_tokens, caller="qa.answer")
        parsed = parse_answer(result.text, result.parsed, passages)
        sources = tuple(SourceRef(p.ref, hit.version_id, hit.chunk_id, p.source, p.section,
                                  p.page_start, p.page_end, hit.score) for hit, p in zip(hits, passages))
        citations = []
        for citation in parsed.citations:
            source = sources[citation.ref - 1]
            citations.append(QaCitation(source.ref, citation.quote, source.version_id, source.chunk_id,
                                        source.source, source.section, source.page_start, source.page_end))
        return QaResult(question, parsed.status.value, parsed.answer, tuple(citations), sources,
                        parsed.dropped, result.usage, result.model_alias)

