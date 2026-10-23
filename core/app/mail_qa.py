"""邮件问答与会话线索的用例（批次 17b）。规格见 docs/spec-31-mail-qa.md。"""
from __future__ import annotations

from dataclasses import dataclass

from core.app.mail import THREAD_LIMIT_MAX, MailHit, MailIndex
from core.app.ports import LlmGateway, LlmUsage, PermanentError, Role, TransientError
from core.domain.mail import MailDoc
from core.domain.mail_qa import NO_MAIL_ANSWER, build_mail_messages, mail_passages, question_keys, thread_key
from core.domain.mail_query import MailQuery
from core.domain.permissions import FolderScope
from core.domain.qa import ANSWER_SCHEMA, parse_answer

CANDIDATES = 30         # 相关度检索的候选数
TOP_K = 6               # 交给模型的邮件数


@dataclass(frozen=True)
class MailSource:
    """交给模型的一封邮件。score：重排分数（没有重排时为 None）。"""
    ref: int
    mail_id: str
    subject: str
    sender: str | None              # 发件人地址
    sent_at: str | None
    folder: str
    score: float | None


@dataclass(frozen=True)
class MailCitation:
    ref: int
    quote: str
    mail_id: str
    subject: str
    sent_at: str | None


@dataclass(frozen=True)
class MailQaResult:
    question: str
    status: str                     # AnswerStatus 的值，或 "no_hits"
    answer: str
    citations: tuple[MailCitation, ...]
    sources: tuple[MailSource, ...]
    dropped_citations: int
    candidates: int                 # 去重后的候选数
    reranked: bool                  # 是否按重排分数排序
    usage: LlmUsage | None
    model: str | None


class MailQaService:
    """ask(question, query, *, scope)：

    1. question 去首尾空白后为空，或 question_keys 为空 → ValueError（不发出任何调用）。query.terms 必须为空（关键词条件
       不用于问答，问题本身即检索词）→ 否则 ValueError。
    2. docs = index.relevant(question, query, scope=scope, limit=candidates)；按 message_id 去重（保留先出现的；
       message_id 为 None 的不去重）。
    3. 没有候选 → status "no_hits"，answer 为 NO_MAIL_ANSWER，不调用模型。
    4. passages = mail_passages(全部候选, question_keys(question))。rerank 为真时
       scores = llm.rerank(question, [p.text …], caller="mail.rerank")：成功 → 候选按分数从高到低稳定排序，reranked 为真；
       抛 PermanentError（例如未配置重排模型）或 TransientError → 保持检索顺序，reranked 为假，不中断问答。
    5. 取前 top_k 封，重新编号 1..k，llm.complete(Role.QA, build_mail_messages(question, 资料), json_schema=ANSWER_SCHEMA,
       temperature=0.0, max_tokens=max_tokens, caller="mail.answer")；异常原样抛出。
    6. parse_answer(result.text, result.parsed, 资料)，每条引用映射回对应邮件。sources 为交给模型的 k 封（score 为重排分数，
       没有重排时 None）；candidates 为去重后的候选数。"""

    def __init__(self, index: MailIndex, llm: LlmGateway, *, candidates: int = CANDIDATES, top_k: int = TOP_K,
                 rerank: bool = True, max_tokens: int = 1024) -> None:
        if not 1 <= top_k <= candidates or max_tokens < 1:
            raise ValueError("候选数、资料数与 max_tokens 必须为正数，且资料数不超过候选数")
        self._index, self._llm = index, llm
        self._candidates, self._top_k, self._rerank, self._max_tokens = candidates, top_k, rerank, max_tokens

    def ask(self, question: str, query: MailQuery = MailQuery(), *, scope: FolderScope | None) -> MailQaResult:
        question = question.strip()
        keys = question_keys(question)
        if not question or not keys:
            raise ValueError("问题不能为空")
        if query.terms:
            raise ValueError("问邮件时不使用关键词条件")
        found = self._index.relevant(question, query, scope=scope, limit=self._candidates)
        seen: set[str] = set()
        docs: list[MailDoc] = []
        for doc in found:
            if doc.message_id is not None:
                if doc.message_id in seen:
                    continue
                seen.add(doc.message_id)
            docs.append(doc)
        if not docs:
            return MailQaResult(question, "no_hits", NO_MAIL_ANSWER, (), (), 0, 0, False, None, None)
        candidates = len(docs)
        passages = mail_passages(docs, keys)
        scores: list[float | None] = [None] * len(docs)
        reranked = False
        if self._rerank:
            try:
                values = self._llm.rerank(question, [p.text for p in passages], caller="mail.rerank")
                if len(values) != len(docs):
                    raise PermanentError("重排结果数量不符")
                order = sorted(range(len(docs)), key=lambda i: -values[i])
                docs = [docs[i] for i in order]
                scores = [values[i] for i in order]
                reranked = True
            except (PermanentError, TransientError):
                pass
        chosen = docs[:self._top_k]
        passages = mail_passages(chosen, keys)
        result = self._llm.complete(Role.QA, build_mail_messages(question, passages), json_schema=ANSWER_SCHEMA,
                                    temperature=0.0, max_tokens=self._max_tokens, caller="mail.answer")
        parsed = parse_answer(result.text, result.parsed, passages)
        sources = tuple(MailSource(ref, doc.mail_id, doc.subject, doc.sender.addr if doc.sender else None, doc.sent_at,
                                   doc.folder, scores[ref - 1]) for ref, doc in enumerate(chosen, 1))
        citations = tuple(MailCitation(c.ref, c.quote, chosen[c.ref - 1].mail_id, chosen[c.ref - 1].subject,
                                       chosen[c.ref - 1].sent_at) for c in parsed.citations)
        return MailQaResult(question, parsed.status.value, parsed.answer, citations, sources, parsed.dropped,
                            candidates, reranked, result.usage, result.model_alias)


@dataclass(frozen=True)
class MailThread:
    """root：会话根的 Message-ID（没有任何 Message-ID 信息时为 None，此时 rows 只有当前邮件）；
    rows：去重后的会话成员（同一 message_id 只留一封：当前邮件本身优先，否则按时间顺序先出现的）；
    truncated：取出的记录达到 THREAD_LIMIT_MAX。"""
    root: str | None
    rows: tuple[MailHit, ...]
    truncated: bool


def mail_thread(index: MailIndex, doc: MailDoc, hit: MailHit, *, scope: FolderScope | None) -> MailThread:
    """doc 所在的会话线索。hit 为 doc 自身的摘要行（调用方由 doc 构造），没有 Message-ID 信息时 rows 只有它。
    成员按 index.thread 的顺序；同一 message_id 的多个副本只留一封：与 doc 同一 message_id 的留 doc 自身（hit），
    其余留先出现的；message_id 为 None 的成员都保留。"""
    key = thread_key(doc)
    if key is None:
        return MailThread(None, (hit,), False)
    members = index.thread(key, scope=scope, limit=THREAD_LIMIT_MAX)
    rows: list[MailHit] = []
    seen: set[str] = set()
    for member in members:
        if member.message_id is not None and member.message_id == doc.message_id:
            member = hit
        if member.message_id is not None:
            if member.message_id in seen:
                continue
            seen.add(member.message_id)
        rows.append(member)
    if doc.message_id is None or doc.message_id not in seen:
        rows.append(hit)            # 当前邮件不在范围内取出的成员里（例如超过上限）时补上
    return MailThread(key.root, tuple(rows), len(members) >= THREAD_LIMIT_MAX)
