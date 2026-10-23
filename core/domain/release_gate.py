"""放行规则（纯逻辑）。规格：docs/spec-02-release-gate.md

放行由程序判断，输入里刻意没有“模型自评分”。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Unmet(str, Enum):
    """未满足的放行条件。枚举定义顺序即输出顺序。"""

    RULES_NOT_PASSED = "rules_not_passed"
    REVIEW_RESULT_INVALID = "review_result_invalid"
    COVERAGE_INCOMPLETE = "coverage_incomplete"
    VISUAL_REVIEW_MISSING = "visual_review_missing"
    BLOCKING_ISSUES_OPEN = "blocking_issues_open"
    HASH_MISMATCH = "hash_mismatch"


@dataclass(frozen=True)
class ReleaseInputs:
    rules_passed: bool
    review_result_valid: bool
    review_units_total: int
    review_units_covered: int
    visual_review_required: bool
    visual_review_done: bool
    blocking_issues_unresolved: int
    artifact_hash: str
    review_bound_hash: str | None


@dataclass(frozen=True)
class ReleaseDecision:
    passed: bool
    unmet: tuple[Unmet, ...]


def evaluate_release(inputs: ReleaseInputs) -> ReleaseDecision:
    """逐条判断，返回所有未满足项（按 Unmet 定义顺序）；passed 当且仅当 unmet 为空。

    各条件：
    - RULES_NOT_PASSED：rules_passed 为 False
    - REVIEW_RESULT_INVALID：review_result_valid 为 False
    - COVERAGE_INCOMPLETE：review_units_total == 0，或 covered < total
    - VISUAL_REVIEW_MISSING：visual_review_required 且 not visual_review_done
    - BLOCKING_ISSUES_OPEN：blocking_issues_unresolved > 0
    - HASH_MISMATCH：review_bound_hash 为 None，或不等于 artifact_hash

    输入非法时抛 ValueError：任一计数为负、covered > total、artifact_hash 为空。
    """
    counts = (
        inputs.review_units_total,
        inputs.review_units_covered,
        inputs.blocking_issues_unresolved,
    )
    if any(count < 0 for count in counts):
        raise ValueError("计数不能为负数")
    if inputs.review_units_covered > inputs.review_units_total:
        raise ValueError("已覆盖单元数不能大于总数")
    if not inputs.artifact_hash:
        raise ValueError("产物哈希不能为空")

    failures = {
        Unmet.RULES_NOT_PASSED: not inputs.rules_passed,
        Unmet.REVIEW_RESULT_INVALID: not inputs.review_result_valid,
        Unmet.COVERAGE_INCOMPLETE: (
            inputs.review_units_total == 0
            or inputs.review_units_covered < inputs.review_units_total
        ),
        Unmet.VISUAL_REVIEW_MISSING: (
            inputs.visual_review_required and not inputs.visual_review_done
        ),
        Unmet.BLOCKING_ISSUES_OPEN: inputs.blocking_issues_unresolved > 0,
        Unmet.HASH_MISMATCH: (
            inputs.review_bound_hash is None
            or inputs.review_bound_hash != inputs.artifact_hash
        ),
    }
    unmet = tuple(condition for condition in Unmet if failures[condition])
    return ReleaseDecision(passed=not unmet, unmet=unmet)
