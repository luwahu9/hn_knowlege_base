"""日期规范化（纯逻辑）。规格：docs/spec-05-dates.md

把模型从原文里抽出的“时间表述”换算成带精度的日期区间。
原则：精度优先于召回——有歧义或缺信息就进“待确认”，不猜。
"""
from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum


class Precision(str, Enum):
    DAY = "day"
    MONTH = "month"
    QUARTER = "quarter"
    HALF_YEAR = "half_year"
    YEAR = "year"


class Issue(str, Enum):
    """无法给出确定日期的原因；出现任何一种都进入“待确认”列表。"""

    EMPTY = "empty"                      # 空串或只有空白
    MISSING_YEAR = "missing_year"        # 缺少年份，如“3月1日”
    MISSING_ANCHOR = "missing_anchor"    # 相对日期但没有提供基准日期
    INVALID_DATE = "invalid_date"        # 带年份的不存在日期、超出年份范围，如“2024年2月30日”
    RANGE_REVERSED = "range_reversed"    # 区间终点早于起点
    UNSUPPORTED = "unsupported"          # 不在支持清单内（含“年底”“上旬”“近期”等）


@dataclass(frozen=True)
class NormalizedDate:
    start: date        # 闭区间起点
    end: date          # 闭区间终点（含）
    precision: Precision


@dataclass(frozen=True)
class DateResult:
    """value 与 issue 恰有一个不为 None。"""

    value: NormalizedDate | None
    issue: Issue | None


YEAR_MIN = 1000
YEAR_MAX = 2999
RANGE_SEPARATORS = "至到~—–-"



def normalize_date(expr: str, *, anchor: date | None = None) -> DateResult:
    """把一条时间表述换算成 NormalizedDate，或给出无法确定的原因。

    预处理：NFKC；去掉全部空白得到 c（用于中文和数字格式）；空白折叠为单个空格得到 sp（用于英文格式）；
    “号”与“日”等价。空串或纯空白 → EMPTY。

    支持清单（逐条见 docs/spec-05-dates.md）：
    1. 绝对日期：2024年3月1日 / 2024年3月1号 / 2024-03-01 / 2024/3/1 / 2024.3.1（三段分隔符必须相同）/ 二〇二四年三月一日
       / March 1, 2024 / 1 March 2024 / Mar 1, 2024。精度 DAY。
    2. 绝对月份：2024年3月 / 2024-03 或 2024-3（只支持连字符）/ March 2024。精度 MONTH。
    3. 年：2024年 / 2024年度。精度 YEAR。
    4. 季度：2024年第一季度 / 2024年第1季度 / 2024年一季度 / 2024年Q1，序号只接受 1–4 或 一二三四（不接受“两”）。精度 QUARTER。
    5. 半年：2024年上半年 / 2024年下半年 / 2024年H1 / 2024年H2。精度 HALF_YEAR。
    6. 相对日期（需要 anchor，缺失 → MISSING_ANCHOR）：
       今天/今日 昨天/昨日 前天 明天/明日 后天（DAY）；本月 上月 下月 及“上个月/下个月/当月”（MONTH）；
       今年 去年 前年 明年 后年 及“本年/上年/下年”（YEAR）；
       N天前/后、N日前/后（DAY）、N个月前/后（MONTH）、N年前/后（YEAR），N 为阿拉伯或中文数字且 ≥ 1。
    7. 区间：左右两端用 至 / 到 / ~ / — / – / - 连接，两端必须是同一精度的绝对表述（不含相对日期和英文格式），
       结果 start 取左端起点，end 取右端终点，精度同两端。
       DAY 区间的右端可省略年（“3月31日”）或年月（“31日”）；MONTH 区间的右端可省略年（“4月”）。
       若有多种拆分方式，取最左边能使两端都解析成功的拆分。终点早于起点 → RANGE_REVERSED。
       右端只在左端解析出结果之后才判断，可用的省略写法取决于左端精度；错误优先级见 spec-05 裁决记录第 5、10 条。
    8. 缺少年份（“3月1日”“3月”）→ MISSING_YEAR，不用 anchor 猜年份。

    带年份的不存在日期（如 2024年2月30日）、不存在的月份（2024年13月）、年份不在 YEAR_MIN..YEAR_MAX → INVALID_DATE。
    缺年份的写法优先判为 MISSING_YEAR，不校验其月、日是否存在（“2月30日”“13月”都是 MISSING_YEAR）。
    中文数字无法按位值解读（“二〇月”“二三月”）视为语法不识别 → UNSUPPORTED。
    其余一切（两位年份、年底/年初、上旬/中旬/下旬、近期、英文区间、混合精度区间等）→ UNSUPPORTED。
    返回值的 value 与 issue 恰有一个不为 None。
    """
    normalized = unicodedata.normalize("NFKC", expr)
    c = "".join(normalized.split()).replace("号", "日")
    sp = " ".join(normalized.split())
    if not c:
        return DateResult(None, Issue.EMPTY)

    absolute = _parse_absolute(c)
    if absolute is not None:
        return absolute
    english = _parse_english(sp)
    if english is not None:
        return english
    relative = _parse_relative(c, anchor)
    if relative is not None:
        return relative
    return _parse_range(c)


_CHINESE_DIGITS: dict[str, int] = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_YEAR_TOKEN = r"(?:[0-9]{4}|[零〇一二两三四五六七八九]{4})"
_NUMBER_TOKEN = (
    r"(?:[0-9]{1,2}|[零〇一二两三四五六七八九]|"
    r"[一二两三四五六七八九]?十[一二两三四五六七八九]?)"
)
_MONTH_NAMES: tuple[str, ...] = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_MONTH_FULL: dict[str, int] = {name: index for index, name in enumerate(_MONTH_NAMES, 1)}
_MONTH_SHORT: dict[str, int] = {
    **{name[:3]: index for index, name in enumerate(_MONTH_NAMES, 1)},
    "sept": 9,
}
_RELATIVE_ALIASES: dict[str, tuple[Precision, int]] = {
    "今天": (Precision.DAY, 0), "今日": (Precision.DAY, 0),
    "昨天": (Precision.DAY, -1), "昨日": (Precision.DAY, -1),
    "前天": (Precision.DAY, -2),
    "明天": (Precision.DAY, 1), "明日": (Precision.DAY, 1),
    "后天": (Precision.DAY, 2),
    "本月": (Precision.MONTH, 0), "当月": (Precision.MONTH, 0),
    "上月": (Precision.MONTH, -1), "上个月": (Precision.MONTH, -1),
    "下月": (Precision.MONTH, 1), "下个月": (Precision.MONTH, 1),
    "今年": (Precision.YEAR, 0), "本年": (Precision.YEAR, 0),
    "去年": (Precision.YEAR, -1), "上年": (Precision.YEAR, -1),
    "前年": (Precision.YEAR, -2),
    "明年": (Precision.YEAR, 1), "下年": (Precision.YEAR, 1),
    "后年": (Precision.YEAR, 2),
}


def _year_number(token: str) -> int:
    """转换已通过语法识别的四位年份。"""
    if token[0] in "0123456789":
        return int(token)
    return int("".join(str(_CHINESE_DIGITS[char]) for char in token))


def _small_number(token: str) -> int:
    """转换已通过语法识别的两位以内整数或中文位值数字。"""
    if token[0] in "0123456789":
        return int(token)
    if "十" not in token:
        return _CHINESE_DIGITS[token]
    tens, _, units = token.partition("十")
    return (_CHINESE_DIGITS[tens] if tens else 1) * 10 + (
        _CHINESE_DIGITS[units] if units else 0
    )


def _date_value(year: int, month: int, day: int, precision: Precision) -> DateResult:
    """校验年份与日历日期，并生成指定精度的闭区间。"""
    if not YEAR_MIN <= year <= YEAR_MAX:
        return DateResult(None, Issue.INVALID_DATE)
    try:
        if precision is Precision.DAY:
            start = end = date(year, month, day)
        else:
            start = date(year, month, 1)
            last_month = month + {
                Precision.MONTH: 0,
                Precision.QUARTER: 2,
                Precision.HALF_YEAR: 5,
                Precision.YEAR: 11,
            }[precision]
            end = date(year, last_month, monthrange(year, last_month)[1])
    except ValueError:
        return DateResult(None, Issue.INVALID_DATE)
    return DateResult(NormalizedDate(start, end, precision), None)


def _parse_absolute(c: str, inherited: NormalizedDate | None = None) -> DateResult | None:
    """识别非英文绝对写法；右端省略的年月仅从已解析的左端取得。"""
    match = re.fullmatch(rf"({_YEAR_TOKEN})年({_NUMBER_TOKEN})月({_NUMBER_TOKEN})日", c)
    if match:
        year, month, day = match.groups()
        return _date_value(_year_number(year), _small_number(month), _small_number(day), Precision.DAY)

    match = re.fullmatch(r"([0-9]{4})([-/.])([0-9]{1,2})\2([0-9]{1,2})", c)
    if match:
        year, _, month, day = match.groups()
        return _date_value(int(year), int(month), int(day), Precision.DAY)

    match = re.fullmatch(rf"({_YEAR_TOKEN})年({_NUMBER_TOKEN})月", c)
    if match:
        year, month = match.groups()
        return _date_value(_year_number(year), _small_number(month), 1, Precision.MONTH)

    match = re.fullmatch(r"([0-9]{4})-([0-9]{1,2})", c)
    if match:
        year, month = match.groups()
        return _date_value(int(year), int(month), 1, Precision.MONTH)

    match = re.fullmatch(rf"({_YEAR_TOKEN})年度?", c)
    if match:
        return _date_value(_year_number(match[1]), 1, 1, Precision.YEAR)

    for pattern, precision, width in (
        (rf"({_YEAR_TOKEN})年(?:第)?([一二三四1-4])季度", Precision.QUARTER, 3),
        (rf"({_YEAR_TOKEN})年[Qq]([1-4])", Precision.QUARTER, 3),
        (rf"({_YEAR_TOKEN})年[Hh]([1-2])", Precision.HALF_YEAR, 6),
    ):
        match = re.fullmatch(pattern, c)
        if match:
            month = (_small_number(match[2]) - 1) * width + 1
            return _date_value(_year_number(match[1]), month, 1, precision)

    match = re.fullmatch(rf"({_YEAR_TOKEN})年([上下])半年", c)
    if match:
        month = 1 if match[2] == "上" else 7
        return _date_value(_year_number(match[1]), month, 1, Precision.HALF_YEAR)

    match = re.fullmatch(rf"({_NUMBER_TOKEN})月(?:({_NUMBER_TOKEN})日)?", c)
    if match:
        if inherited is None:
            return DateResult(None, Issue.MISSING_YEAR)
        month, day = match.groups()
        precision = Precision.DAY if day is not None else Precision.MONTH
        if precision is not inherited.precision:
            return None
        return _date_value(
            inherited.start.year, _small_number(month),
            _small_number(day) if day is not None else 1, precision,
        )

    if inherited is not None and inherited.precision is Precision.DAY:
        match = re.fullmatch(rf"({_NUMBER_TOKEN})日", c)
        if match:
            return _date_value(
                inherited.start.year, inherited.start.month, _small_number(match[1]), Precision.DAY,
            )
    return None


def _english_month(token: str) -> int | None:
    """只接受完整月份名称、三字母缩写及 Sept；句点仅用于缩写。"""
    token = token.lower()
    if token.endswith("."):
        return _MONTH_SHORT.get(token[:-1])
    return _MONTH_FULL.get(token, _MONTH_SHORT.get(token))


def _parse_english(sp: str) -> DateResult | None:
    """识别三种英文格式，保留必要空白和规定的逗号位置。"""
    match = re.fullmatch(r"([A-Za-z]+\.?),? ([0-9]{1,2}),? ([0-9]{4})", sp)
    if match:
        name, day, year = match.groups()
    else:
        match = re.fullmatch(r"([0-9]{1,2}) ([A-Za-z]+\.?),? ([0-9]{4})", sp)
        if match:
            day, name, year = match.groups()
        else:
            match = re.fullmatch(r"([A-Za-z]+\.?) ([0-9]{4})", sp)
            if not match:
                return None
            name, year = match.groups()
            month = _english_month(name)
            if month is None:
                return None
            return _date_value(int(year), month, 1, Precision.MONTH)
    month = _english_month(name)
    if month is None:
        return None
    return _date_value(int(year), month, int(day), Precision.DAY)


def _parse_relative(c: str, anchor: date | None) -> DateResult | None:
    """识别相对词或有限数量的偏移，仅使用传入的基准日期。"""
    offset = _RELATIVE_ALIASES.get(c)
    if offset is None:
        match = re.fullmatch(rf"({_NUMBER_TOKEN})(天|日|个月|年)(前|后)", c)
        if not match:
            return None
        token, unit, direction = match.groups()
        amount = _small_number(token)
        if amount < 1:
            return None
        precision = {"天": Precision.DAY, "日": Precision.DAY, "个月": Precision.MONTH, "年": Precision.YEAR}[unit]
        offset = (precision, -amount if direction == "前" else amount)
    if anchor is None:
        return DateResult(None, Issue.MISSING_ANCHOR)

    precision, amount = offset
    if precision is Precision.DAY:
        try:
            target = anchor + timedelta(days=amount)
        except OverflowError:
            return DateResult(None, Issue.INVALID_DATE)
        return _date_value(target.year, target.month, target.day, precision)
    if precision is Precision.MONTH:
        year, month = divmod(anchor.year * 12 + anchor.month - 1 + amount, 12)
        return _date_value(year, month + 1, 1, precision)
    return _date_value(anchor.year + amount, 1, 1, precision)


def _parse_range(c: str) -> DateResult:
    """依次尝试每个分隔符，保留首个识别错误，成功区间优先返回。"""
    first_issue: Issue | None = None
    for index, char in enumerate(c):
        if char not in RANGE_SEPARATORS:
            continue
        left = _parse_absolute(c[:index])
        if left is None:
            continue
        if left.value is None:
            if first_issue is None:
                first_issue = left.issue
            continue
        right = _parse_absolute(c[index + 1:], left.value)
        if right is None:
            continue
        if right.value is None:
            if first_issue is None:
                first_issue = right.issue
            continue
        if left.value.precision is not right.value.precision:
            continue
        if right.value.end < left.value.start:
            return DateResult(None, Issue.RANGE_REVERSED)
        return DateResult(
            NormalizedDate(left.value.start, right.value.end, left.value.precision), None,
        )
    return DateResult(None, first_issue if first_issue is not None else Issue.UNSUPPORTED)
