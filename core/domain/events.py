"""业务事件校验（纯逻辑）。规格：docs/spec-07-events.md

模型抽出的每条事件草稿，必须由原文引文支撑；引文找不到、字段不在引文里，一律作废。
"""
from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import Enum

from core.domain.dates import DateResult, NormalizedDate, normalize_date
from core.domain.doc_period import Period, resolve_with_period
from core.domain.entities import normalize_name
from core.domain.quote_check import QuoteMatch, locate_quote


@dataclass(frozen=True)
class EventDraft:
    """模型输出的一条事件草稿（字段均为模型给出的原文片段）。"""

    subject: str                 # 主体
    action: str                  # 行为（允许模型归纳，不要求逐字出现在引文中）
    object: str | None           # 对象
    time_expr: str | None        # 时间表述（原文写法）
    place: str | None            # 地点
    quote: str                   # 证据引文


class Problem(str, Enum):
    """枚举定义顺序即输出顺序。"""

    QUOTE_NOT_FOUND = "quote_not_found"
    SUBJECT_MISSING = "subject_missing"
    ACTION_MISSING = "action_missing"
    SUBJECT_NOT_IN_QUOTE = "subject_not_in_quote"
    OBJECT_NOT_IN_QUOTE = "object_not_in_quote"
    TIME_NOT_IN_QUOTE = "time_not_in_quote"
    PLACE_NOT_IN_QUOTE = "place_not_in_quote"
    TIME_MISSING = "time_missing"
    DATE_UNRESOLVED = "date_unresolved"
    QUOTE_AMBIGUOUS = "quote_ambiguous"


REJECT_PROBLEMS: frozenset[Problem] = frozenset({
    Problem.QUOTE_NOT_FOUND, Problem.SUBJECT_MISSING, Problem.ACTION_MISSING,
    Problem.SUBJECT_NOT_IN_QUOTE, Problem.OBJECT_NOT_IN_QUOTE,
    Problem.TIME_NOT_IN_QUOTE, Problem.PLACE_NOT_IN_QUOTE,
})


class Verdict(str, Enum):
    PASS = "pass"        # 所有检查通过，可按策略自动确认或抽检
    REVIEW = "review"    # 证据成立但信息不全或有歧义，进入事件审核
    REJECT = "reject"    # 证据不成立，丢弃并记录


@dataclass(frozen=True)
class EventCheck:
    verdict: Verdict
    problems: tuple[Problem, ...]      # 按 Problem 定义顺序，去重
    quote_match: QuoteMatch | None     # 引文在 source_text 中的位置
    date: DateResult | None            # time_expr 为空时为 None


def check_event(draft: EventDraft, *, source_text: str, anchor: date | None, period: Period | None = None) -> EventCheck:
    """校验一条事件草稿。所有检查都执行，问题全部列出（不提前返回）。

    - QUOTE_NOT_FOUND：locate_quote(draft.quote, source_text) 返回 None（按默认 min_chars，太短也算找不到）。
    - QUOTE_AMBIGUOUS：引文找到但 occurrences > 1。
    - SUBJECT_MISSING / ACTION_MISSING：去空白后为空。
    - SUBJECT_NOT_IN_QUOTE：主体非空，但不在引文中。
    - OBJECT_NOT_IN_QUOTE / PLACE_NOT_IN_QUOTE / TIME_NOT_IN_QUOTE：该字段非空（去空白后），但不在引文中。
      “在引文中”的判定：locate_quote(字段, draft.quote, min_chars=1) 不为 None
      （与引文校验同样的规范化：NFKC、忽略空白、区分大小写）。
    - TIME_MISSING：time_expr 为空（None 或去空白后为空）；此时 date 为 None。
    - DATE_UNRESOLVED：time_expr 非空，但日期结果有 issue。日期结果：period 为 None 时是 normalize_date(time_expr, anchor=anchor)，
      否则是 resolve_with_period(time_expr, period, anchor=anchor)（spec-23）；date 字段始终保存该结果（time_expr 非空时）。
    - action 不要求出现在引文中（允许模型把“到访”归纳为“访问”）。

    verdict：problems 与 REJECT_PROBLEMS 有交集 → REJECT；否则 problems 非空 → REVIEW；否则 PASS。
    """
    problems: set[Problem] = set()
    quote_match = locate_quote(draft.quote, source_text)
    if quote_match is None:
        problems.add(Problem.QUOTE_NOT_FOUND)
    elif quote_match.occurrences > 1:
        problems.add(Problem.QUOTE_AMBIGUOUS)

    if not draft.subject.strip():
        problems.add(Problem.SUBJECT_MISSING)
    elif locate_quote(draft.subject, draft.quote, min_chars=1) is None:
        problems.add(Problem.SUBJECT_NOT_IN_QUOTE)
    if not draft.action.strip():
        problems.add(Problem.ACTION_MISSING)

    for field, problem in (
        (draft.object, Problem.OBJECT_NOT_IN_QUOTE),
        (draft.time_expr, Problem.TIME_NOT_IN_QUOTE),
        (draft.place, Problem.PLACE_NOT_IN_QUOTE),
    ):
        if field is not None and field.strip() and locate_quote(field, draft.quote, min_chars=1) is None:
            problems.add(problem)

    date_result: DateResult | None = None
    if draft.time_expr is None or not draft.time_expr.strip():
        problems.add(Problem.TIME_MISSING)
    else:
        date_result = (normalize_date(draft.time_expr, anchor=anchor) if period is None
                       else resolve_with_period(draft.time_expr, period, anchor=anchor))
        if date_result.issue is not None:
            problems.add(Problem.DATE_UNRESOLVED)

    if problems & REJECT_PROBLEMS:
        verdict = Verdict.REJECT
    elif problems:
        verdict = Verdict.REVIEW
    else:
        verdict = Verdict.PASS
    return EventCheck(
        verdict=verdict,
        problems=tuple(problem for problem in Problem if problem in problems),
        quote_match=quote_match,
        date=date_result,
    )


def event_fingerprint(subject: str, action: str, object_: str | None, when: NormalizedDate | None) -> str:
    """事件指纹，用于新版本重抽后识别“同一事件”以继承已确认结果。

    各部分：
    - 主体、对象：normalize_name；对象为 None 或去空白后为空 → 空串
    - 行为：NFKC、去掉全部空白、casefold
    - 时间：f"{start.isoformat()}/{end.isoformat()}/{precision.value}"，None → 空串
    四部分按 主体、行为、对象、时间 的顺序用 "\\x1f" 连接，UTF-8 编码后取 sha256 十六进制小写。
    主体或行为规范化后为空 → ValueError。
    """
    subject_key = normalize_name(subject)
    action_key = "".join(unicodedata.normalize("NFKC", action).split()).casefold()
    if not subject_key or not action_key:
        raise ValueError("主体和行为规范化后均不能为空")

    object_key = normalize_name(object_) if object_ is not None and object_.strip() else ""
    time_key = (
        f"{when.start.isoformat()}/{when.end.isoformat()}/{when.precision.value}"
        if when is not None else ""
    )
    content = "\x1f".join((subject_key, action_key, object_key, time_key))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
