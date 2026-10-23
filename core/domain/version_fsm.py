"""文档版本状态机（纯逻辑，不做任何 I/O）。

规格：docs/spec-01-version-state-machine.md
TRANSITIONS 是唯一的转移表；除 ADMIN_RETRY 外，状态变化只能由它决定。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class VersionState(str, Enum):
    REGISTERED = "registered"          # 已登记
    CONVERTING = "converting"          # 转换中
    QC = "qc"                          # 质检中
    MODEL_REVIEW = "model_review"      # 模型审核中
    INGESTING = "ingesting"            # 入库中
    PUBLISHED = "published"            # 已发布
    SUPERSEDED = "superseded"          # 已替代（终态）
    PENDING_MANUAL = "pending_manual"  # 待人工审核
    EXCEPTION = "exception"            # 异常队列
    REJECTED = "rejected"              # 已拒绝（终态）
    REVISED = "revised"                # 已修订，由新版本接替（终态）


class Event(str, Enum):
    START_CONVERSION = "start_conversion"
    CONVERSION_OK = "conversion_ok"
    RULES_PASSED = "rules_passed"
    RULES_FAILED = "rules_failed"
    GATE_PASSED = "gate_passed"
    GATE_FAILED = "gate_failed"
    INGEST_VERIFIED = "ingest_verified"
    SUPERSEDE = "supersede"
    HUMAN_APPROVED_AS_IS = "human_approved_as_is"
    HUMAN_REVISED = "human_revised"
    HUMAN_REJECTED = "human_rejected"
    STEP_FAILED = "step_failed"        # 自动重试用尽后触发
    ADMIN_RETRY = "admin_retry"
    ADMIN_REJECT = "admin_reject"


S = VersionState
E = Event

# 可自动执行、失败后可进入异常队列并重试的步骤
RETRYABLE_STEPS: frozenset[VersionState] = frozenset(
    {S.CONVERTING, S.QC, S.MODEL_REVIEW, S.INGESTING}
)

TRANSITIONS: dict[tuple[VersionState, Event], VersionState] = {
    (S.REGISTERED, E.START_CONVERSION): S.CONVERTING,
    (S.CONVERTING, E.CONVERSION_OK): S.QC,
    (S.QC, E.RULES_PASSED): S.MODEL_REVIEW,
    (S.QC, E.RULES_FAILED): S.PENDING_MANUAL,
    (S.MODEL_REVIEW, E.GATE_PASSED): S.INGESTING,
    (S.MODEL_REVIEW, E.GATE_FAILED): S.PENDING_MANUAL,
    (S.INGESTING, E.INGEST_VERIFIED): S.PUBLISHED,
    (S.PUBLISHED, E.SUPERSEDE): S.SUPERSEDED,
    (S.PENDING_MANUAL, E.HUMAN_APPROVED_AS_IS): S.INGESTING,
    (S.PENDING_MANUAL, E.HUMAN_REVISED): S.REVISED,
    (S.PENDING_MANUAL, E.HUMAN_REJECTED): S.REJECTED,
    (S.CONVERTING, E.STEP_FAILED): S.EXCEPTION,
    (S.QC, E.STEP_FAILED): S.EXCEPTION,
    (S.MODEL_REVIEW, E.STEP_FAILED): S.EXCEPTION,
    (S.INGESTING, E.STEP_FAILED): S.EXCEPTION,
    (S.EXCEPTION, E.ADMIN_REJECT): S.REJECTED,
    # (EXCEPTION, ADMIN_RETRY) 不在表内：目标是 failed_step，由 next_state 特殊处理
}

TERMINAL_STATES: frozenset[VersionState] = frozenset({S.SUPERSEDED, S.REJECTED, S.REVISED})
REASON_REQUIRED: frozenset[Event] = frozenset({E.HUMAN_REJECTED, E.ADMIN_REJECT})


class InvalidTransition(Exception):
    """当前状态下不允许该事件。"""


@dataclass(frozen=True)
class Transition:
    from_state: VersionState
    to_state: VersionState
    event: Event
    actor: str
    at: datetime
    reason: str | None = None
    failed_step: VersionState | None = None


def next_state(
    state: VersionState, event: Event, failed_step: VersionState | None = None
) -> VersionState:
    """返回事件发生后的状态。

    - 不在转移表中的 (state, event) 抛 InvalidTransition。
    - ADMIN_RETRY 仅允许在 EXCEPTION 状态；必须给出属于 RETRYABLE_STEPS 的
      failed_step，返回它；缺失或不合法抛 ValueError；状态不是 EXCEPTION 抛 InvalidTransition。
    """
    if event is E.ADMIN_RETRY:
        if state is not S.EXCEPTION:
            raise InvalidTransition("只有异常队列中的版本可以重试")
        if failed_step is None or failed_step not in RETRYABLE_STEPS:
            raise ValueError("重试必须指定合法的失败步骤")
        return failed_step

    try:
        return TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransition("当前状态不允许该事件") from None


def allowed_events(state: VersionState) -> frozenset[Event]:
    """该状态下合法的事件集合（含 EXCEPTION 的 ADMIN_RETRY）。终态返回空集合。"""
    events = frozenset(event for current, event in TRANSITIONS if current == state)
    if state is S.EXCEPTION:
        return events | frozenset({E.ADMIN_RETRY})
    return events


def is_terminal(state: VersionState) -> bool:
    return state in TERMINAL_STATES


def initial_state(*, from_revision: bool) -> VersionState:
    """新版本的初始状态：人工修订产生的版本直接从质检开始，其余从已登记开始。"""
    return S.QC if from_revision else S.REGISTERED


def apply_event(
    current: VersionState,
    event: Event,
    *,
    actor: str,
    at: datetime,
    reason: str | None = None,
    failed_step: VersionState | None = None,
) -> Transition:
    """校验并生成一条审计记录（不修改任何外部状态）。

    检查顺序：
    1. actor 去空白后为空 → ValueError
    2. event 属于 REASON_REQUIRED 且 reason 去空白后为空 → ValueError
    3. 调用 next_state（可能抛 InvalidTransition / ValueError）
    STEP_FAILED 的记录里 failed_step 自动取 current，忽略传入值。
    ADMIN_RETRY 的记录里 failed_step 为传入值。其余事件 failed_step 为 None。
    """
    if not actor.strip():
        raise ValueError("操作者不能为空")
    if event in REASON_REQUIRED and (reason is None or not reason.strip()):
        raise ValueError("拒绝必须填写原因")

    target = next_state(current, event, failed_step)
    if event is E.STEP_FAILED:
        recorded_step = current
    elif event is E.ADMIN_RETRY:
        recorded_step = failed_step
    else:
        recorded_step = None

    return Transition(
        from_state=current,
        to_state=target,
        event=event,
        actor=actor,
        at=at,
        reason=reason,
        failed_step=recorded_step,
    )
