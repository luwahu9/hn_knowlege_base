"""批次 23c：文档系列（纯逻辑）。规格见 docs/spec-42-document-series.md。

系列是周报、月报、日报、简报这类定期出版物的各期。认定方式（用户决定 2026-10-23）：标题规则自动归组，审核员可以调整。
- 自动：标题去掉日期、期号、前缀代码与“发送、终版、链接版”等后缀，得到系列名；同一文件夹内系列名相同的文档达到
  MIN_SERIES_SIZE 份即成为一个自动系列。
- 调整：四种审核决定（新建系列、改名、合并、把文档归入某系列或移出），复用审核决定的存储、撤销与事件。
系列不单独存储，每次由文档记录与决定计算得出（`build_series`）。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.domain.lifecycle import PUBLISHED, document_key
from core.domain.review import LOCAL_ACTOR, Decision, DecisionKind, ReviewError, decision_id, decision_number, latest

MIN_SERIES_SIZE = 3            # 自动系列至少几份文档
MAX_NAME_CHARS = 60
MIN_KEY_CHARS = 2

_OPEN, _CLOSE = "（(【[〔", "）)】]〕"
_NOISE = ("带链接版", "链接版", "修改稿", "终版", "定稿", "草稿", "副本", "发送", "修改", "改")
_CN_DIGITS = "〇零一二三四五六七八九十百两"
_DATE_PATTERNS = [
    r"(19|20)\d{2}\s*[-－—–~～至到]\s*(19|20)\d{2}\s*年?",              # 2018-2020年
    r"(19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]?",          # 2024年10月13日
    r"(19|20)\d{2}[.\-_/]\d{1,2}[.\-_/]\d{1,2}",                      # 2024.07.13 / 2024-7-13
    r"(19|20)\d{2}\s*年\s*\d{1,2}\s*月",                              # 2024年10月
    r"(?<!\d)(19|20)\d{6}(?!\d)",                                     # 20200203
    r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]?",                              # 9月13日
    r"\d{1,2}\s*[日号]",                                              # 18日
    r"(?<!\d)\d{1,2}\.\d{1,2}(?!\d)",                                 # 6.16
    r"(?<!\d)\d{4}(?!\d)",                                            # 0813、2024
    r"(19|20)\d{2}\s*年",                                             # 2024年
    r"第\s*[" + _CN_DIGITS + r"\d]+\s*[期号]",                        # 第十期、第2期
]
_DATE = re.compile("|".join(f"(?:{p})" for p in _DATE_PATTERNS))
_RANGE_JOIN = re.compile(r"[\s\-－—–~～至到]+")
_EXTENSION = re.compile(r"\.(docx?|pdf|xlsx?|pptx?|txt|md|html?|wps|eml)$", re.IGNORECASE)
_LEADING_CODE_SEP = re.compile(r"^[A-Za-z]{1,8}\d*\s*[-_]+\s*")         # “sfgxs-”“ZJ-”“zjwz-”
_LEADING_CODE = re.compile(r"^[A-Za-z]{1,6}\d*\s*(?=[\u4e00-\u9fff])")    # “SF9”“hub”紧贴汉字；“SF2024”去掉年份后留下的空白也算
_LEADING_DIGITS = re.compile(r"^\d+\s*[-_.、]?\s*")                       # “02”“20200203-”
_TRAILING_CODE = re.compile(r"[-_]\s*[A-Za-z0-9]{1,8}$")                  # “-wqy”“-10h”
_EDGE = "-－—–_~～、，,。.·:：;；/\\|　 \"'"
_PAIRS = ("()", "【】", "[]", "〔〕", "“”", "‘’", "《》", "（）")


def _strip_brackets(text: str) -> str:
    """去掉内容含数字、日期或噪声词的括号段（开闭括号可以不配对，如“（2024.07.13)”）；其他括号保留。"""
    pattern = re.compile("[" + re.escape(_OPEN) + "]([^" + re.escape(_OPEN + _CLOSE) + "]*)[" + re.escape(_CLOSE) + "]")

    def drop(match: re.Match[str]) -> str:
        inner = match.group(1)
        noisy = re.search(r"\d", inner) or any(word in inner for word in _NOISE) or re.search(
            "第[" + _CN_DIGITS + "]+[期号]", inner)
        return " " if noisy else match.group(0)

    previous = None
    while previous != text:
        previous, text = text, pattern.sub(drop, text)
    return text


def _trim(text: str) -> str:
    """去掉两端的标点与空白；两端的括号、引号只在不配对时去掉（“南海“核爆”事件分析”中的引号保留）。"""
    previous = None
    while previous != text:
        previous = text = text.strip(_EDGE)
        for opener, closer in _PAIRS:
            if text.startswith(opener) and text.count(opener) > text.count(closer):
                text = text[1:]
            if text.endswith(closer) and text.count(closer) > text.count(opener):
                text = text[:-1]
            if text.endswith(opener) and opener != closer:               # 结尾的开括号总是残留（如“动态（”）
                text = text[:-1]
            if text.startswith(closer) and opener != closer:
                text = text[1:]
            if len(text) > 2 and text[0] == opener and text[-1] == closer and text.count(opener) == 1:
                text = text[1:-1]                                   # 整个名称外层的一对：“《南海周报》”
    return text


def series_name(title: str) -> str | None:
    """标题对应的系列名（显示用）；去掉噪声后不足 MIN_KEY_CHARS 个字符（不计空白）→ None。步骤：
    1. NFKC；去掉文件扩展名。
    2. 去掉内容含数字、噪声词（_NOISE）或期号的括号段，开闭括号可以不配对。
    3. 去掉日期、日期区间、年份、四位数字（月日）与期号（_DATE），以及区间之间的连接符。
    4. 去掉前缀代码：先去“字母[数字][-_]”形式的前缀（如“sfgxs-”），没有时再去紧贴汉字的“字母[数字]”（如“SF9”“hub”）；
       再去开头的数字编号（如“02”“20200203-”）。
    5. 反复去掉结尾的噪声词、“-字母数字”代码与两端的标点、空白；内部连续空白合成一个。
    例：“SF9全球生物安全领域重要动态2024年10月13日）” → “全球生物安全领域重要动态”；
        “20200319-南海舆情周报（0313-0319第十期）发送” → “南海舆情周报”；“0101-0107NH” → “NH”。"""
    text = _EXTENSION.sub("", unicodedata.normalize("NFKC", title).strip())
    text = _strip_brackets(text)
    text = _DATE.sub(" ", text)
    text = re.sub(r"(?<=\s)[\-－—–~～至到]+(?=\s)", " ", f" {text} ")
    text = _trim(text)
    stripped = _LEADING_CODE_SEP.sub("", text, count=1)
    if stripped == text:
        stripped = _LEADING_CODE.sub("", text, count=1)
    text = _trim(_LEADING_DIGITS.sub("", _trim(stripped), count=1))
    previous = None
    while previous != text:
        previous = text
        for word in _NOISE:
            if text.endswith(word) and len(text) > len(word):
                text = text[: -len(word)]
        text = _trim(_TRAILING_CODE.sub("", text))
    text = re.sub(r"\s+", " ", text)
    return text if len(re.sub(r"\s", "", text)) >= MIN_KEY_CHARS else None


def series_key(name: str) -> str:
    """归组用的键：去掉全部空白后 casefold。"""
    return re.sub(r"\s", "", name).casefold()


def auto_series_id(folder: str, key: str) -> str:
    return "ser_" + hashlib.sha256(f"{folder}\x00{key}".encode("utf-8")).hexdigest()[:12]


def manual_series_id(number: int) -> str:
    return f"ser_m{number:06d}"


def document_target(record: Mapping[str, Any]) -> str:
    """文档在决定中的目标：f"{文件夹}/{NFC casefold 文件名}"（根文件夹为 "/文件名"）。同一文档的各版本相同。"""
    folder, filename = document_key(record)
    return f"{folder}/{filename}"


@dataclass(frozen=True)
class Series:
    id: str
    name: str
    auto: bool                            # True：标题规则得出；False：人工新建
    folder: str | None                    # 自动系列所在文件夹；人工系列为 None
    auto_name: str | None                 # 规则给出的名称（人工系列为 None）
    members: tuple[str, ...]              # version_id，按期次排序（见 build_series）

    def to_json(self, members: Sequence[str] | None = None) -> dict[str, Any]:
        shown = list(self.members if members is None else members)
        return {"id": self.id, "name": self.name, "auto": self.auto, "folder": self.folder,
                "auto_name": self.auto_name, "count": len(shown)}


@dataclass(frozen=True)
class SeriesView:
    series: tuple[Series, ...]                       # 至少有一个成员的系列，按 (name, id) 排序
    of_version: Mapping[str, str]                    # version_id → 系列 ID（只含成员）
    redirects: Mapping[str, str] = field(default_factory=dict)   # 被合并的系列 ID → 最终系列 ID
    known: frozenset[str] = frozenset()              # 存在的系列 ID（含暂时没有成员的人工系列与合并前的 ID）
    names: Mapping[str, str] = field(default_factory=dict)       # known 中每个系列的当前名称（已叠加改名）

    def get(self, series_id: str) -> Series | None:
        final = self.redirects.get(series_id, series_id)
        return next((row for row in self.series if row.id == final), None)


def _issue_key(record: Mapping[str, Any], position: int) -> tuple[Any, ...]:
    period = record.get("period") or {}
    start = period.get("start")
    return (start is None, start or "", position)


def build_series(records: Sequence[Mapping[str, Any]], decisions: Iterable[Any]) -> SeriesView:
    """由文档记录（documents() 的顺序）与审核决定（Decision，只看四种系列决定的 latest()）计算系列。
    1. 成员候选：status 为 published 的记录；同一文档（document_target 相同）有多个已发布版本时只取最后保存的一个。
    2. 自动系列：series_name(title) 不为 None 的候选，按 (folder, series_key) 分组，达到 MIN_SERIES_SIZE 的组成为
       自动系列，ID 为 auto_series_id(folder, key)，auto_name 为组内出现最多的系列名（并列取最先出现的）。
    3. 人工系列：每个 SERIES_CREATE 决定（target 为 manual_series_id，value 为名称）。
    4. 归属：候选先归入其自动系列（如有）；DOCUMENT_SERIES（target 为 document_target，value 为系列 ID 或 ""）覆盖：
       "" → 不属于任何系列；value 是存在的系列（自动或人工）→ 归入它；否则忽略。自动系列的成立只看第 2 步的计数，
       移出文档不会使它解散。
    5. 合并：SERIES_MERGE（target 并入 value），两端都必须存在；按决定编号依次接受，会形成环的忽略；链式合并取最终 ID。
       被合并系列的成员归入最终系列，redirects 记下映射。
    6. 名称：最终系列的 SERIES_RENAME 决定 → 其值；否则人工系列为新建时的名称，自动系列为 auto_name。
       针对被合并系列的改名不生效。
    7. 期次排序：period.start 升序（没有时间段的排在最后），再按保存先后。没有成员的系列不出现在 series 中。"""
    newest = latest(decisions)
    candidates: dict[str, tuple[int, Mapping[str, Any]]] = {}
    for position, record in enumerate(records):
        if record.get("status") == PUBLISHED:
            candidates[document_target(record)] = (position, record)
    groups: dict[tuple[str, str], list[tuple[int, Mapping[str, Any], str]]] = {}
    for position, record in candidates.values():
        name = series_name(record.get("title") or record.get("filename") or "")
        if name is not None:
            groups.setdefault((record.get("folder", ""), series_key(name)), []).append((position, record, name))
    base: dict[str, Series] = {}
    assigned: dict[str, str] = {}                           # document_target → 系列 ID
    for (folder, key), rows in groups.items():
        if len(rows) < MIN_SERIES_SIZE:
            continue
        series_id = auto_series_id(folder, key)
        names = Counter(name for _, _, name in rows)
        top = max(names.values())
        auto_name = next(name for _, _, name in sorted(rows, key=lambda row: row[0]) if names[name] == top)
        base[series_id] = Series(series_id, auto_name, True, folder, auto_name, ())
        for _, record, _ in rows:
            assigned[document_target(record)] = series_id
    for (group, target), decision in newest.items():
        if decision.kind is DecisionKind.SERIES_CREATE:
            base[target] = Series(target, decision.value, False, None, None, ())
    for (group, target), decision in newest.items():
        if decision.kind is DecisionKind.DOCUMENT_SERIES and target in candidates:
            if decision.value == "":
                assigned.pop(target, None)
            elif decision.value in base:
                assigned[target] = decision.value
    redirects: dict[str, str] = {}
    merges = sorted((d for d in newest.values() if d.kind is DecisionKind.SERIES_MERGE),
                    key=lambda d: decision_number(d.decision_id))

    def final(series_id: str) -> str:
        while series_id in redirects:
            series_id = redirects[series_id]
        return series_id

    for decision in merges:
        source, target = decision.target, decision.value
        if source in base and target in base and source not in redirects and final(target) != source:
            redirects[source] = target
    redirects = {source: final(source) for source in redirects}
    members: dict[str, list[tuple[tuple[Any, ...], str]]] = {}
    of_version: dict[str, str] = {}
    for target, series_id in assigned.items():
        position, record = candidates[target]
        series_id = redirects.get(series_id, series_id)
        members.setdefault(series_id, []).append((_issue_key(record, position), record["version_id"]))
        of_version[record["version_id"]] = series_id
    names: dict[str, str] = {}
    for series_id, series in base.items():
        rename = newest.get(("series_name", series_id))
        names[series_id] = rename.value if rename is not None and rename.kind is DecisionKind.SERIES_RENAME else series.name
    result = []
    for series_id, rows in members.items():
        series = base[series_id]
        result.append(Series(series_id, names[series_id], series.auto, series.folder, series.auto_name,
                             tuple(version for _, version in sorted(rows))))
    result.sort(key=lambda row: (row.name, row.id))
    return SeriesView(tuple(result), of_version, redirects, frozenset(base), names)


def clean_series_name(value: str | None) -> str:
    """人工给出的系列名：去首尾空白、内部连续空白合成一个；为空或超过 MAX_NAME_CHARS → ValueError。"""
    text = re.sub(r"\s+", " ", (value or "").strip())
    if not text:
        raise ValueError("系列名称不能为空")
    if len(text) > MAX_NAME_CHARS:
        raise ValueError(f"系列名称不能超过 {MAX_NAME_CHARS} 字")
    return text


def make_series_decision(kind: DecisionKind, target: str, value: str | None, *, view: SeriesView,
                         records: Sequence[Mapping[str, Any]], number: int, created_at: str,
                         actor: str = LOCAL_ACTOR) -> Decision:
    """校验并生成一条系列决定（不保存）。view 为当前（已叠加全部决定的）系列视图，records 为全部文档记录。
    目标不存在 → KeyError；不允许 → ReviewError。snapshot 一律为 ()。
    - SERIES_CREATE：target 为 "new"（接口要求 target 非空；"" 也接受）或 manual_series_id(number)，结果一律为后者；value 经
      clean_series_name（不合法 → ReviewError）。label “新建系列：{名称}”。
    - SERIES_RENAME：target 必须是 view 中有成员的系列（被合并的 ID 也算不存在）→ 否则 KeyError；value 经 clean_series_name。
      label “系列改名：{原名} → {新名}”。
    - SERIES_MERGE：target、value 都必须是 view 中有成员的系列（否则 KeyError），且不同 → ReviewError。
      label “合并系列：{target 名} → {value 名}”。
    - DOCUMENT_SERIES：target 必须是某条已发布记录的 document_target（否则 KeyError）；value 为 "" 或 view.known 中、
      未被合并的系列 ID（否则 ReviewError；value 为 None → ReviewError）；与文档当前所属系列相同 → ReviewError（无变化）。
      label “归入系列：{标题} → {系列名}”或“移出系列：{标题}”（标题取该文档最后保存的已发布版本）。
    其他 kind → ReviewError。"""
    id_ = decision_id(number)
    names = {row.id: row.name for row in view.series}
    if kind is DecisionKind.SERIES_CREATE:
        expected = manual_series_id(number)
        if target not in ("", "new", expected):
            raise ReviewError("新建系列的编号由系统分配")
        name = _name(value)
        return Decision(id_, kind, expected, name, (), f"新建系列：{name}", actor, created_at)
    if kind is DecisionKind.SERIES_RENAME:
        if target not in names:
            raise KeyError(target)
        name = _name(value)
        return Decision(id_, kind, target, name, (), f"系列改名：{names[target]} → {name}", actor, created_at)
    if kind is DecisionKind.SERIES_MERGE:
        if target not in names or value not in names:
            raise KeyError(target if target not in names else str(value))
        if target == value:
            raise ReviewError("不能把系列并入它自己")
        return Decision(id_, kind, target, value, (), f"合并系列：{names[target]} → {names[value]}", actor, created_at)
    if kind is DecisionKind.DOCUMENT_SERIES:
        documents = [row for row in records if row.get("status") == PUBLISHED and document_target(row) == target]
        if not documents:
            raise KeyError(target)
        if value is None:
            raise ReviewError("请选择系列")
        if value and (value not in view.known or value in view.redirects):
            raise ReviewError("系列不存在或已被合并")
        record = documents[-1]
        if view.of_version.get(record["version_id"], "") == value:
            raise ReviewError("文档已经在该系列中")
        title = record.get("title") or record.get("filename") or record["version_id"]
        if value:
            label = f"归入系列：{title} → {view.names.get(value, value)}"
        else:
            label = f"移出系列：{title}"
        return Decision(id_, kind, target, value, (), label, actor, created_at)
    raise ReviewError("不支持的审核操作")


def _name(value: str | None) -> str:
    try:
        return clean_series_name(value)
    except ValueError as exc:
        raise ReviewError(str(exc)) from None

