"""实体对齐（纯逻辑）。规格：docs/spec-06-entities.md

原则：宁可漏合并，也不错合并。
只有“规范化后完全同名且唯一”或“命中人工确认过的别名”才自动归并；
相似但不相同的名字一律进人工确认；没有任何相似实体才新建。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum


class EntityType(str, Enum):
    """批次 23a 起共 9 种（中文名与子类见 core/domain/taxonomy.py）。原来的 "media" 并入组织（子类“媒体”），
    旧数据读取时由 taxonomy.parse_type 换算。"""
    PERSON = "person"
    ORGANIZATION = "organization"
    COUNTRY = "country"                  # 国家与地区（含行政区域）
    LOCATION = "location"                # 地理位置
    NAMED_EVENT = "named_event"          # 命名事件
    LEGAL = "legal"                      # 法律与条约
    EQUIPMENT = "equipment"              # 装备与设施
    HONOR = "honor"                      # 荣誉
    GROUP = "group"                      # 群体


class Reason(str, Enum):
    """进入人工确认的理由。枚举定义顺序即排序顺序。"""

    EXACT_BUT_AMBIGUOUS = "exact_but_ambiguous"    # 完全同名，但对应多个实体或该实体被标记为同名多义
    SAME_IGNORING_SEPARATORS = "same_ignoring_separators"  # 去掉间隔号、空格、连字符后相同
    SAME_ORG_CORE = "same_org_core"                # 去掉公司后缀后相同（仅组织；批次 23a 起媒体并入组织）
    SAME_TOKENS = "same_tokens"                    # 按空格切分的多词名字，词相同、顺序不同（不限文字）
    CONTAINS = "contains"                          # 一个名字包含另一个（较短者至少 2 个字符）


class ResolutionKind(str, Enum):
    MATCHED = "matched"
    NEW = "new"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class Entity:
    id: str
    type: EntityType
    canonical: str                 # 规范名（人工确认）
    aliases: frozenset[str]        # 人工确认过的别名（原样保存）
    ambiguous: bool = False        # 人工标记：存在同名不同实体，完全同名也不得自动归并


@dataclass(frozen=True)
class Candidate:
    entity_id: str
    reasons: tuple[Reason, ...]    # 按 Reason 定义顺序，去重


@dataclass(frozen=True)
class Resolution:
    kind: ResolutionKind
    key: str                                   # 提及名称的规范化结果
    entity_id: str | None = None               # 仅 MATCHED 时有值
    via: str | None = None                     # 仅 MATCHED：“canonical” 或 “alias”
    candidates: tuple[Candidate, ...] = ()     # 仅 NEEDS_REVIEW：按 entity_id 升序


QUOTE_CHARS = "\"'“”‘’《》「」『』"
MIDDLE_DOTS = "•・‧∙⋅"
ORG_SUFFIXES: tuple[str, ...] = (
    "集团股份有限公司", "股份有限公司", "有限责任公司", "集团有限公司", "有限公司", "集团", "公司",
    "co., ltd.", "co., ltd", "co.,ltd.", "co.,ltd", "corporation", "limited", "company",
    "corp.", "corp", "inc.", "inc", "ltd.", "ltd", "llc", "plc", "gmbh", "co.", "co",
)
MIN_CONTAINS_CHARS = 2


def normalize_name(name: str) -> str:
    """名称规范化（用于比较的键，不用于展示）。

    依次：NFKC；去掉首尾空白；反复去掉首尾的引号、书名号（QUOTE_CHARS 中的字符）及其间空白；
    把 MIDDLE_DOTS 中的字符统一为“·”（U+00B7）；去掉“·”两侧的空白；连续空白折叠为一个空格；
    去掉首尾空白；casefold。结果可能为空串。
    """
    key = unicodedata.normalize("NFKC", name).strip()
    while True:
        unquoted = key.strip(QUOTE_CHARS).strip()
        if unquoted == key:
            break
        key = unquoted
    key = key.translate(str.maketrans({char: "·" for char in MIDDLE_DOTS}))
    key = re.sub(r"\s*·\s*", "·", key)
    return " ".join(key.split()).casefold()


def org_core(key: str) -> str:
    """去掉组织名末尾的公司后缀（参数应为 normalize_name 的结果）。

    反复执行，直到不再匹配：按 ORG_SUFFIXES 的顺序找第一个满足以下全部条件的后缀——
    (1) key 以它结尾；(2) 对以 ASCII 字符开头的后缀（英文后缀），去掉后缀后剩余部分的最后一个字符必须是空格或逗号
    （避免 "costco" 被当成 "cost" + "co"）；(3) 去掉后缀、再去掉剩余部分末尾的空格和逗号之后，结果非空。
    找到就用该结果替换 key 并继续下一轮；某个后缀不满足条件时继续尝试下一个后缀。
    中文后缀不要求边界（“华为技术有限公司” → “华为技术”）。
    每一轮开始时，若当前结果本身就是 ORG_SUFFIXES 中的一项（如“有限公司”），立即停止并返回它；
    因此 key 本身是后缀时原样返回，“股份有限公司有限公司”得到“股份有限公司”而不是“股份”。
    """
    while key not in ORG_SUFFIXES:
        for suffix in ORG_SUFFIXES:
            if not key.endswith(suffix):
                continue
            remainder = key[:-len(suffix)]
            if suffix[0].isascii() and (not remainder or remainder[-1] not in " ,"):
                continue
            core = remainder.rstrip(" ,")
            if not core:
                continue
            key = core
            break
        else:
            return key
    return key


def similarity_reasons(a: str, b: str, etype: EntityType) -> tuple[Reason, ...]:
    """两个规范化名称（均为 normalize_name 结果）之间的相似理由；a == b 时返回空元组
    （完全相同不属于“相似”，由 resolve 单独处理）。

    - SAME_IGNORING_SEPARATORS：去掉“·”、空白、“-”后相同。
    - SAME_ORG_CORE：仅 ORGANIZATION；两者 org_core 相同。
    - SAME_TOKENS：两者都按单个空格切分且都至少 2 个词，词的多重集合相同。不限字符范围（中文、带重音字母均可），
      词按规范化后的原样比较，不再拆分连字符或句点（“jean-paul” 是一个词）。
    - CONTAINS：一个是另一个的子串，且较短者长度 ≥ MIN_CONTAINS_CHARS。
    结果按 Reason 定义顺序。
    """
    if a == b:
        return ()

    reasons: set[Reason] = set()
    if re.sub(r"[·\s-]", "", a) == re.sub(r"[·\s-]", "", b):
        reasons.add(Reason.SAME_IGNORING_SEPARATORS)
    if etype is EntityType.ORGANIZATION and org_core(a) == org_core(b):
        reasons.add(Reason.SAME_ORG_CORE)
    a_tokens = a.split(" ")
    b_tokens = b.split(" ")
    if len(a_tokens) >= 2 and len(b_tokens) >= 2 and sorted(a_tokens) == sorted(b_tokens):
        reasons.add(Reason.SAME_TOKENS)
    if min(len(a), len(b)) >= MIN_CONTAINS_CHARS and (a in b or b in a):
        reasons.add(Reason.CONTAINS)
    return tuple(reason for reason in Reason if reason in reasons)


def resolve(name: str, etype: EntityType, entities: tuple[Entity, ...]) -> Resolution:
    """把一个提及名称对齐到已有实体。只与同类型实体比较。

    0. 前置条件：entities 中的 id 必须唯一（不论类型、内容是否相同）；出现重复 id → ValueError，
       不得自行去重或合并。该检查在其他任何判断之前进行。
    1. key = normalize_name(name)；为空 → ValueError。
    2. 完全匹配：同类型实体中，规范名或任一别名的规范化结果等于 key。
       - 恰好 1 个且未标记 ambiguous → MATCHED；规范名匹配时 via="canonical"，否则 via="alias"。
       - 2 个及以上，或 1 个但 ambiguous → NEEDS_REVIEW，候选为这些实体，理由均为 (EXACT_BUT_AMBIGUOUS,)。
    3. 没有完全匹配：对每个同类型实体，取其规范名和所有别名与 key 的 similarity_reasons 的并集；
       有任一理由即为候选。有候选 → NEEDS_REVIEW；没有 → NEW。
    候选按 entity_id 升序；每个候选的理由去重并按 Reason 定义顺序。
    """
    seen: set[str] = set()
    for entity in entities:
        if entity.id in seen:
            raise ValueError(f"实体 id 重复：{entity.id}")
        seen.add(entity.id)

    key = normalize_name(name)
    if not key:
        raise ValueError("提及名称规范化后不能为空")

    normalized: list[tuple[Entity, str, frozenset[str]]] = []
    exact: list[tuple[Entity, str]] = []
    for entity in entities:
        if entity.type != etype:
            continue
        canonical = normalize_name(entity.canonical)
        aliases = frozenset(normalize_name(alias) for alias in entity.aliases)
        normalized.append((entity, canonical, aliases))
        if canonical == key:
            exact.append((entity, "canonical"))
        elif key in aliases:
            exact.append((entity, "alias"))

    if exact:
        if len(exact) == 1 and not exact[0][0].ambiguous:
            entity, via = exact[0]
            return Resolution(ResolutionKind.MATCHED, key, entity.id, via)
        return Resolution(
            ResolutionKind.NEEDS_REVIEW,
            key,
            candidates=tuple(
                Candidate(entity.id, (Reason.EXACT_BUT_AMBIGUOUS,))
                for entity, _ in sorted(exact, key=lambda match: match[0].id)
            ),
        )

    candidates: list[Candidate] = []
    for entity, canonical, aliases in normalized:
        reasons: set[Reason] = set()
        for entity_name in (canonical, *aliases):
            reasons.update(similarity_reasons(key, entity_name, etype))
        if reasons:
            candidates.append(Candidate(entity.id, tuple(reason for reason in Reason if reason in reasons)))
    if candidates:
        return Resolution(
            ResolutionKind.NEEDS_REVIEW,
            key,
            candidates=tuple(sorted(candidates, key=lambda candidate: candidate.entity_id)),
        )
    return Resolution(ResolutionKind.NEW, key)
