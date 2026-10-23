"""用例：文档连线——引用提取与冲突判断。规格：docs/spec-18-links.md"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core.app.ports import Chunk, LlmGateway, Role
from core.domain.knowledge import EventRecord
from core.domain.qa import strip_breadcrumb
from core.domain.relations import (JUDGE_SCHEMA, REFERENCE_SCHEMA, ConflictRecord, ConflictVerdict, ReferenceRecord,
                                   build_judge_messages, build_reference_messages, normalize_title, pair_id,
                                   parse_judgement, parse_references)

REFS_CALLER = "kg.refs"
JUDGE_CALLER = "kg.conflict"
FORMAT_ERROR_REASON = "模型输出格式异常"


@dataclass(frozen=True)
class ReferenceReport:
    version_id: str
    chunks: int                  # 实际调用模型的分块数（正文为空的分块不计）
    invalid_outputs: int         # parse_references 返回 ok=False 的分块数
    dropped_items: int           # 各分块 References.dropped 之和
    self_references: int         # 因引用自己的标题而丢弃的条目数
    references: int              # 生成的 ReferenceRecord 数
    prompt_tokens: int
    completion_tokens: int
    model: str | None            # 最后一次调用的 model_alias；没有调用时为 None

    def to_json(self) -> dict[str, Any]:
        """字段名与属性名相同的扁平字典。"""
        return dict(vars(self))


@dataclass(frozen=True)
class DocumentReferences:
    version_id: str
    references: tuple[ReferenceRecord, ...]
    report: ReferenceReport


class LinkService:
    def __init__(self, llm: LlmGateway, *, max_tokens: int = 2048) -> None:
        self._llm = llm
        self._max_tokens = max_tokens

    def extract_references(self, version_id: str, title: str, chunks: Sequence[Chunk]) -> DocumentReferences:
        """逐分块提取引用与替代。chunks 按给定顺序处理。

        对每个分块：
        1. body = strip_breadcrumb(chunk.text)；body 去空白后为空 → 跳过（不调用模型、不计入 chunks）。
        2. llm.complete(Role.EXTRACT, build_reference_messages(title, chunk.section_path, body), json_schema=REFERENCE_SCHEMA,
           temperature=0.0, max_tokens=max_tokens, caller=REFS_CALLER)。模型调用抛出的任何异常原样向上抛出，不返回部分结果。
        3. ex = parse_references(result.text, result.parsed, body)。ok 为 False → invalid_outputs += 1，处理下一分块；
           否则 dropped_items += ex.dropped。
        4. 对 ex.items 依次处理：normalize_title(item.title) == normalize_title(title) → self_references += 1，跳过；
           否则版本内序号 n += 1（从 1 起），生成 ReferenceRecord(f"{version_id}:r{n:04d}", version_id, chunk.chunk_id, item.kind,
           item.reverse, item.title, item.quote, item.start, item.end, chunk.page_start, chunk.page_end, chunk.section_path)。
        token 用量为各次调用之和；model 为最后一次调用的 model_alias。
        """
        records: list[ReferenceRecord] = []
        calls = invalid = dropped = self_refs = prompt_tokens = completion_tokens = 0
        model = None
        for chunk in chunks:
            body = strip_breadcrumb(chunk.text)
            if not body.strip():
                continue
            result = self._llm.complete(Role.EXTRACT, build_reference_messages(title, chunk.section_path, body),
                                        json_schema=REFERENCE_SCHEMA, temperature=0.0, max_tokens=self._max_tokens,
                                        caller=REFS_CALLER)
            calls += 1
            prompt_tokens += result.usage.prompt_tokens
            completion_tokens += result.usage.completion_tokens
            model = result.model_alias
            ex = parse_references(result.text, result.parsed, body)
            if not ex.ok:
                invalid += 1
                continue
            dropped += ex.dropped
            for item in ex.items:
                if normalize_title(item.title) == normalize_title(title):
                    self_refs += 1
                    continue
                records.append(ReferenceRecord(f"{version_id}:r{len(records) + 1:04d}", version_id, chunk.chunk_id,
                                               item.kind, item.reverse, item.title, item.quote, item.start, item.end,
                                               chunk.page_start, chunk.page_end, chunk.section_path))
        report = ReferenceReport(version_id, calls, invalid, dropped, self_refs, len(records),
                                 prompt_tokens, completion_tokens, model)
        return DocumentReferences(version_id, tuple(records), report)

    def judge(self, first: EventRecord, second: EventRecord, titles: Mapping[str, str]) -> ConflictRecord:
        """判断一对事件是否冲突。两个 event_id 相同 → ValueError（在调用模型之前）。

        按 event_id 升序排成 (a, b)；标题取 titles.get(version_id, version_id)。
        llm.complete(Role.REVIEW, build_judge_messages(标题a, a.quote, 标题b, b.quote), json_schema=JUDGE_SCHEMA,
        temperature=0.0, max_tokens=max_tokens, caller=JUDGE_CALLER)；模型调用的异常原样向上抛。
        parse_judgement(result.text, result.parsed, a.quote, b.quote)：ok 为 False →
        verdict=REVIEW、reason=FORMAT_ERROR_REASON、两个 detail 为空；否则取其 verdict、reason、detail_a、detail_b。
        返回 ConflictRecord(pair_id(a.event_id, b.event_id), a.event_id, b.event_id, a.version_id, b.version_id, …)。
        """
        key = pair_id(first.event_id, second.event_id)
        a, b = sorted((first, second), key=lambda event: event.event_id)
        result = self._llm.complete(Role.REVIEW,
            build_judge_messages(titles.get(a.version_id, a.version_id), a.quote, titles.get(b.version_id, b.version_id), b.quote),
            json_schema=JUDGE_SCHEMA, temperature=0.0, max_tokens=self._max_tokens, caller=JUDGE_CALLER)
        judgement = parse_judgement(result.text, result.parsed, a.quote, b.quote)
        if not judgement.ok:
            return ConflictRecord(key, a.event_id, b.event_id, a.version_id, b.version_id,
                                  ConflictVerdict.REVIEW, FORMAT_ERROR_REASON, "", "")
        return ConflictRecord(key, a.event_id, b.event_id, a.version_id, b.version_id,
                              judgement.verdict, judgement.reason, judgement.detail_a, judgement.detail_b)
