"""批次 23a：实体分类（一级类型与子类）。规格见 docs/spec-40-entity-taxonomy.md。

一级类型决定图谱上的颜色与筛选、实体对齐的范围（只与同类型比较）；子类只用于展示、检索与筛选，不参与对齐。
子类取值必须属于该类型的列表；模型给出的子类不在列表中时记为 None（未分类），不丢弃实体。
"""
from __future__ import annotations

from core.domain.entities import EntityType

TYPE_NAMES: dict[EntityType, str] = {
    EntityType.PERSON: "人物",
    EntityType.ORGANIZATION: "组织",
    EntityType.COUNTRY: "国家与地区",
    EntityType.LOCATION: "地理位置",
    EntityType.NAMED_EVENT: "命名事件与项目",            # 批次 23b：原“命名事件”，加入项目与计划
    EntityType.LEGAL: "法律与条约",
    EntityType.EQUIPMENT: "装备与设施",
    EntityType.HONOR: "荣誉",
    EntityType.GROUP: "群体",
}
TYPE_LABELS: dict[str, EntityType] = {name: type_ for type_, name in TYPE_NAMES.items()}

SUBTYPES: dict[EntityType, tuple[str, ...]] = {
    EntityType.PERSON: ("政要", "政府官员", "军人", "专家学者", "外交人员", "企业家", "其他"),
    EntityType.ORGANIZATION: ("政府部门", "监管机构", "军队", "情报机构", "政党", "智库", "高校与科研机构", "企业",
                              "社会团体与基金会", "国际组织", "媒体", "其他"),
    EntityType.COUNTRY: ("国家", "行政区域"),
    EntityType.LOCATION: ("自然区域", "地点", "边界线"),
    EntityType.NAMED_EVENT: ("会议", "袭击", "危机", "军事行动", "项目与计划", "其他"),
    EntityType.LEGAL: ("法律", "法规", "协议", "条约"),
    EntityType.EQUIPMENT: ("设备", "武器", "通信设施", "产品"),
    EntityType.HONOR: ("奖项", "称号"),
    EntityType.GROUP: ("民族", "国籍群体"),
}

# 批次 23a 之前的类型值：读取旧数据时换算（"media" → 组织 / 媒体）。
LEGACY_TYPES: dict[str, tuple[EntityType, str | None]] = {"media": (EntityType.ORGANIZATION, "媒体")}


def normalize_subtype(type_: EntityType, value: object) -> str | None:
    """value 是字符串且去首尾空白后属于 SUBTYPES[type_] → 该值；否则 None。"""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value in SUBTYPES[type_] else None


def parse_type(value: str) -> tuple[EntityType, str | None]:
    """存储中的类型值 → (类型, 旧值隐含的子类)。旧值见 LEGACY_TYPES；其他值按 EntityType 解析（无效 → ValueError）。"""
    if value in LEGACY_TYPES:
        return LEGACY_TYPES[value]
    return EntityType(value), None


# 批次 23a-r2：容易混淆的子类的判定口径（写进提示词，评测用例按同一口径给出标准答案）。
SUBTYPE_RULES: tuple[str, ...] = (
    "国家元首、政府首脑、部长级及以上官员（含外交部长、国防部长）归“政要”；驻外大使、领事等使领馆人员归“外交人员”；"
    "其他公务员归“政府官员”",
    "国家、国际组织之间的条约、公约、协定归“条约”；企业、机构之间的合同性协议归“协议”",
    "河流、山脉、海域、海峡等自然对象归“自然区域”，即使原文把它用作边界；只有专门命名的边界线（如“某某线”）归“边界线”",
)


def prompt_types() -> str:
    """提示词中的类型说明：每个类型一行 “- 类型：子类、子类…”，随后是 SUBTYPE_RULES（每条一行，以“子类口径：”开头）。"""
    lines = [f"- {TYPE_NAMES[type_]}：{'、'.join(subtypes)}" for type_, subtypes in SUBTYPES.items()]
    return "\n".join(lines + [f"   子类口径：{rule}。" for rule in SUBTYPE_RULES])
