"""文档时间段与缺年份日期补全（纯逻辑）。规格：docs/spec-23-real-docs.md

简报、周报里的日期大多不写年份（“9日”“1月1日”）。文档本身的时间段通常能从文件名、标题或所在文件夹推出，
据此为缺年份的日期补年份；推不出来就保持“缺年份”，不猜。
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum

from core.domain.dates import DateResult, Issue, NormalizedDate, normalize_date


class PeriodSource(str, Enum):
    NAME = "name"          # 文件名或标题中的完整日期 / 带年份的区间
    FOLDER = "folder"      # 月日来自文件名或标题，年份来自所在文件夹名；或只有文件夹年份
    CONTENT = "content"    # 月日来自文件名或标题，年份由正文中落在该时间段内的完整日期确认


@dataclass(frozen=True)
class Period:
    start: date
    end: date              # 含当天
    source: PeriodSource
    year_only: bool = False   # 只知道年份（整年时间段）

    def to_json(self) -> dict[str, object]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(),
                "source": self.source.value, "year_only": self.year_only}


YEAR_MIN, YEAR_MAX = 1990, 2099
_Y = r"(19\d{2}|20\d{2})"
_FULL_DATE = [
    re.compile(_Y + r"年(\d{1,2})月(\d{1,2})[日号]"),
    re.compile(_Y + r"[.\-_/](\d{1,2})[.\-_/](\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)" + _Y + r"(\d{2})(\d{2})(?!\d)"),
]
_MD = r"(\d{1,2})月(\d{1,2})[日号]?"
_RANGE_MD = [   # 无年份的月日区间：(m1, d1, m2 或 None, d2)
    re.compile(_MD + r"\s*[-－—–~～至到]\s*(?:(\d{1,2})月)?(\d{1,2})[日号]?"),
    re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\s*[-－—–~～至到]\s*(?:(\d{1,2})\.)?(\d{1,2})(?![\d.])"),
    re.compile(r"(?<!\d)(\d{2})(\d{2})\s*[-－—–~～]\s*(\d{2})(\d{2})(?!\d)"),
]
_YEAR_ANY = re.compile(r"(?<!\d)" + _Y + r"(?!\d)")
_CONTENT_DATE = re.compile(_Y + r"年(\d{1,2})月(\d{1,2})[日号]")
CONTENT_SCAN_CHARS = 20000


def _date(y: int, m: int, d: int) -> date | None:
    if not YEAR_MIN <= y <= YEAR_MAX:
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _full_dates(text: str) -> list[date]:
    matches = sorted((match.start(), match) for pattern in _FULL_DATE for match in pattern.finditer(text))
    result: list[date] = []
    for _, match in matches:
        value = _date(*(int(part) for part in match.groups()))
        if value is not None and value not in result:
            result.append(value)
    return result


def _month_day_range(text: str) -> tuple[int, int, int, int] | None:
    matches = sorted((match.start(), match) for pattern in _RANGE_MD for match in pattern.finditer(text))
    for _, match in matches:
        m1, d1, m2, d2 = match.groups()
        md = (int(m1), int(d1), int(m2 or m1), int(d2))
        if _date(2000, md[0], md[1]) is not None and _date(2000, md[2], md[3]) is not None:
            return md
    return None


def _range_in_year(md: tuple[int, int, int, int], year: int) -> tuple[date, date] | None:
    m1, d1, m2, d2 = md
    start = _date(year, m1, d1)
    end = _date(year + int((m2, d2) < (m1, d1)), m2, d2)
    return (start, end) if start is not None and end is not None else None


def document_period(name: str, folders: tuple[str, ...], title: str, body: str) -> Period | None:
    """推断文档时间段。name 为文件名（含扩展名），folders 为相对路径中的各级文件夹名（自顶向下），
    title 为文档标题，body 为正文（只看前 CONTENT_SCAN_CHARS 个字符）。全部先做 NFKC。

    依次尝试，命中即返回：
    1. 文件名（去扩展名）、标题中的完整日期（YYYY年M月D日、YYYY.M.D / YYYY-M-D / YYYY_M_D / YYYY/M/D、YYYYMMDD）：
       取最早出现的两个不同日期中较早者为 start、较晚者为 end（只有一个则 start = end），source NAME。
    2. 文件名、标题中的无年份月日区间（“9月5日-12日”“1月1日至1月7日”“10.16-11.15”“0101-0107”）：
       年份依次取 a) 文件名或标题中独立出现的四位年份；b) 由近到远各级文件夹名中独立出现的四位年份（source FOLDER）；
       c) 正文中“YYYY年M月D日”形式的日期投票：对每个这样的日期，分别以其年份 Y 和 Y-1 作为候选起始年构造区间，
          日期落在 [区间起点 - 7 天, 区间终点 + 7 天] 内则该候选起始年得一票（跨年区间因此能被下一年的正文日期确认）；
          取票数最多的起始年（并列取较大者），source CONTENT。a) 的 source 为 NAME。区间终点月日早于起点时终点算下一年。都没有 → 继续。
    3. 文件名、标题、各级文件夹名中独立出现的四位年份（按此顺序取第一个）：整年时间段，year_only=True，
       source 为 NAME（文件名或标题）或 FOLDER。
    4. 都没有 → None。年份须在 YEAR_MIN–YEAR_MAX；不存在的日期视为不匹配。
    """
    name = unicodedata.normalize("NFKC", name)
    name = name.rsplit(".", 1)[0] if "." in name else name
    title = unicodedata.normalize("NFKC", title)
    folders = tuple(unicodedata.normalize("NFKC", folder) for folder in folders)
    body = unicodedata.normalize("NFKC", body)[:CONTENT_SCAN_CHARS]
    names = (name, title)
    dates: list[date] = []
    for text in names:
        for value in _full_dates(text):
            if value not in dates:
                dates.append(value)
    if dates:
        return Period(min(dates[:2]), max(dates[:2]), PeriodSource.NAME)

    year_sources = [(text, PeriodSource.NAME) for text in names]
    year_sources.extend((folder, PeriodSource.FOLDER) for folder in reversed(folders))
    years = [(int(match[1]), source) for text, source in year_sources for match in _YEAR_ANY.finditer(text)
             if YEAR_MIN <= int(match[1]) <= YEAR_MAX]
    md = next((value for text in names if (value := _month_day_range(text)) is not None), None)
    if md is not None:
        if years:
            year, source = years[0]
            interval = _range_in_year(md, year)
            if interval is not None:
                return Period(*interval, source)
        else:
            votes: Counter[int] = Counter()
            for match in _CONTENT_DATE.finditer(body):
                value = _date(*(int(part) for part in match.groups()))
                if value is None:
                    continue
                for year in (value.year, value.year - 1):
                    interval = _range_in_year(md, year)
                    if interval is not None and interval[0] - timedelta(days=7) <= value <= interval[1] + timedelta(days=7):
                        votes[year] += 1
            if votes:
                year = max(votes, key=lambda candidate: (votes[candidate], candidate))
                return Period(*_range_in_year(md, year), PeriodSource.CONTENT)
    if years:
        year, source = years[0]
        return Period(date(year, 1, 1), date(year, 12, 31), source, True)
    return None


def period_from_json(data: Mapping[str, object]) -> Period:
    """Period.to_json 的逆运算。start、end、source 为必填键（缺少 → KeyError）；year_only 可选，缺省 False。
    日期格式或 source 无效 → ValueError。"""
    return Period(date.fromisoformat(str(data["start"])), date.fromisoformat(str(data["end"])),
                  PeriodSource(data["source"]), bool(data.get("year_only", False)))


# ---------------------------------------------------------------- 补全日期
_DAY_ONLY = re.compile(r"^(\d{1,2}|[一二三]?十[一二三四五六七八九]?|[一二三四五六七八九])[日号]$")
PAST_DAYS = 183      # 非整年时间段：允许事件早于时间段起点的天数
FUTURE_DAYS = 31     # 允许晚于时间段终点的天数
DAY_ONLY_DAYS = 31   # “N日”只在时间段终点之前这么多天内找


def _cn_number(text: str) -> int:
    if text.isdecimal():
        return int(text)
    digits = "零一二三四五六七八九"
    if "十" in text:
        tens, units = text.split("十")
        return (digits.index(tens) if tens else 1) * 10 + (digits.index(units) if units else 0)
    return digits.index(text)


def resolve_with_period(expr: str, period: Period, *, anchor: date | None = None) -> DateResult:
    """用文档时间段补全缺年份的日期。

    1. r = normalize_date(expr, anchor=anchor 或（时间段为单日时）period.start)。r 有值，或 issue 不是
       MISSING_YEAR / UNSUPPORTED → 返回 r。
    2. 只有“N日 / N号”（N 为 1–31 的阿拉伯或中文数字，NFKC、去空白后整体匹配）且时间段不是整年：
       取不晚于 period.end、且不早于 period.end - DAY_ONLY_DAYS 天的最近一个“当月 N 日” → DAY 精度；没有 → 返回 r。
    3. r.issue 为 MISSING_YEAR：对 period.start.year - 1 至 period.end.year + 1 的每个年份 y，
       解析 normalize_date(f"{y}年" + 去空白的 expr)；保留有值、且与窗口有交集的结果。
       窗口：整年时间段为 [period.start, period.end]；否则为 [period.start - PAST_DAYS 天, period.end + FUTURE_DAYS 天]。
       恰好一个 → 返回它；否则返回 r。
    4. 其余 → 返回 r。
    """
    reference = anchor if anchor is not None else period.start if period.start == period.end else None
    result = normalize_date(expr, anchor=reference)
    if result.value is not None or result.issue not in {Issue.MISSING_YEAR, Issue.UNSUPPORTED}:
        return result
    compact = "".join(unicodedata.normalize("NFKC", expr).split())
    match = _DAY_ONLY.fullmatch(compact)
    if match is not None and not period.year_only:
        day = _cn_number(match[1])
        if 1 <= day <= 31:
            for offset in range(DAY_ONLY_DAYS + 1):
                candidate = period.end - timedelta(days=offset)
                if candidate.day == day:
                    return normalize_date(f"{candidate.year}年{candidate.month}月{day}日")
        return result
    if result.issue is Issue.MISSING_YEAR:
        start = period.start if period.year_only else period.start - timedelta(days=PAST_DAYS)
        end = period.end if period.year_only else period.end + timedelta(days=FUTURE_DAYS)
        candidates: list[DateResult] = []
        for year in range(period.start.year - 1, period.end.year + 2):
            candidate = normalize_date(f"{year}年{compact}")
            if candidate.value is not None and candidate.value.start <= end and candidate.value.end >= start:
                candidates.append(candidate)
        if len(candidates) == 1:
            return candidates[0]
    return result
