"""邮件检索条件（批次 17a）。规格见 docs/spec-30-mail.md 第四节。纯逻辑。"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from core.domain.folders import in_folder, normalize_folder_path
from core.domain.mail import CHINA, Address, MailDoc, any_matches, mail_tokens, term_matches

MAX_TERMS = 10
MAX_TERM_CHARS = 100
PAGE_LIMIT_MAX = 100
RESULT_WINDOW = 10_000          # offset + limit 的上限（ES 的 max_result_window）
TOTAL_CAP = 10_000              # 总数超过时只报告 TOTAL_CAP，并标记不精确


@dataclass(frozen=True)
class MailQuery:
    """全部条件同时满足（AND）。各字段为 None 或空元组表示不限。

    terms：关键词；每个都要在主题、正文、附件名、发件人或收件人的显示名之一中出现（mail.term_matches）。
    sender：含 "@" 时与发件人地址完全相同（不分大小写）；否则在发件人显示名中出现（term_matches）。
    recipient：同 sender 的规则，对 To、Cc、Bcc 中任一地址。
    start / end：发送日期（UTC+08:00 的日期）的闭区间；有日期条件时没有发送时间的邮件不出现。
    folder：邮件所在文件夹位于该文件夹之内（含自身与子孙，folders.in_folder）。
    has_attachments：True 只要有附件的，False 只要没有附件的。"""
    terms: tuple[str, ...] = ()
    sender: str | None = None
    recipient: str | None = None
    start: date | None = None
    end: date | None = None
    folder: str | None = None
    has_attachments: bool | None = None


def parse_terms(text: str) -> tuple[str, ...]:
    """把检索框的输入拆成关键词：双引号括起的部分整体为一个关键词（可含空格），其余按空白拆分；
    每个关键词去首尾空白，空的丢弃，重复的（完全相同）只留第一个，保持顺序。

    以下情况 ValueError（消息为中文，可直接显示给用户）：
    - 关键词超过 MAX_TERMS 个，或某个超过 MAX_TERM_CHARS 个字符；
    - 某个关键词没有任何词元（mail_tokens 为空，例如只有标点）；
    - 某个关键词只由一个中日韩字符构成（二元切分检索不到单个汉字）：消息为“关键词至少需要两个汉字”。
    未闭合的引号：引号之后的部分按普通文本处理（引号本身丢弃）。"""
    terms: list[str] = []
    for quoted, plain in re.findall(r'"([^"]*)"|([^\s"]+)', text):
        term = (quoted or plain).strip()
        if term and term not in terms:
            terms.append(term)
    if len(terms) > MAX_TERMS:
        raise ValueError(f"关键词最多 {MAX_TERMS} 个")
    for term in terms:
        if len(term) > MAX_TERM_CHARS:
            raise ValueError(f"关键词不能超过 {MAX_TERM_CHARS} 个字符")
        tokens = mail_tokens(term)
        if not tokens:
            raise ValueError(f"关键词“{term}”没有可检索的文字")
        if len(tokens) == 1 and tokens[0].cjk:
            raise ValueError("关键词至少需要两个汉字")
    return tuple(terms)


def _person(value: str | None, label: str) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if len(value) > MAX_TERM_CHARS:
        raise ValueError(f"{label}不能超过 {MAX_TERM_CHARS} 个字符")
    if "@" in value:
        return "".join(value.split()).lower()
    tokens = mail_tokens(value)
    if not tokens:
        raise ValueError(f"{label}没有可检索的文字")
    if len(tokens) == 1 and tokens[0].cjk:
        raise ValueError(f"{label}至少需要两个汉字")
    return value


def build_query(*, q: str = "", sender: str | None = None, recipient: str | None = None, start: str | None = None,
                end: str | None = None, folder: str | None = None, attachments: str | None = None) -> MailQuery:
    """由接口参数组装 MailQuery（参数都是字符串，空串视同未给出）。不合规 → ValueError（中文消息）。

    - q → parse_terms。
    - sender / recipient：去首尾空白；含 "@" 时去掉全部空白并转小写；不含 "@" 时按关键词的规则检查（无词元、单个汉字 → ValueError），
      原样保留（去首尾空白后）。超过 MAX_TERM_CHARS → ValueError。
    - start / end：ISO 日期（YYYY-MM-DD）；start 晚于 end → ValueError。
    - folder：normalize_folder_path；规范化后为根 "" 时视同不限（None）。
    - attachments："1" → True，"0" → False，其他非空值 → ValueError。"""
    try:
        first = date.fromisoformat(start) if start else None
        last = date.fromisoformat(end) if end else None
    except ValueError as exc:
        raise ValueError("日期格式应为 YYYY-MM-DD") from exc
    if first and last and first > last:
        raise ValueError("开始日期晚于结束日期")
    path = normalize_folder_path(folder) if folder else ""
    if attachments in (None, ""):
        has = None
    elif attachments in ("1", "0"):
        has = attachments == "1"
    else:
        raise ValueError("附件条件只能是 1 或 0")
    return MailQuery(parse_terms(q), _person(sender, "发件人"), _person(recipient, "收件人"), first, last,
                     path or None, has)


def check_page(offset: int, limit: int) -> None:
    """offset ≥ 0、1 ≤ limit ≤ PAGE_LIMIT_MAX、offset + limit ≤ RESULT_WINDOW，否则 ValueError。"""
    if offset < 0 or not 1 <= limit <= PAGE_LIMIT_MAX:
        raise ValueError(f"分页参数无效（limit 取 1–{PAGE_LIMIT_MAX}）")
    if offset + limit > RESULT_WINDOW:
        raise ValueError(f"只能翻到前 {RESULT_WINDOW} 条结果，请缩小检索范围")


def _person_matches(wanted: str, people: Sequence[Address]) -> bool:
    if "@" in wanted:
        return any(person.addr == wanted for person in people)
    return any(term_matches(wanted, person.name) for person in people)


def mail_matches(doc: MailDoc, query: MailQuery) -> bool:
    """参照语义（内存实现与契约测试以此为准）：doc 是否满足 query 的全部条件，规则见 MailQuery。
    关键词的检索字段：subject、body、各附件名、发件人与 To/Cc/Bcc 的显示名。"""
    texts = [doc.subject, doc.body, *(item.name for item in doc.attachments),
             *(person.name for person in ([doc.sender] if doc.sender else []) + list(doc.to + doc.cc + doc.bcc))]
    if not all(any_matches(term, texts) for term in query.terms):
        return False
    if query.sender is not None and not _person_matches(query.sender, [doc.sender] if doc.sender else []):
        return False
    if query.recipient is not None and not _person_matches(query.recipient, doc.to + doc.cc + doc.bcc):
        return False
    if query.start is not None or query.end is not None:
        if doc.sent_at is None:
            return False
        day = datetime.fromisoformat(doc.sent_at).astimezone(CHINA).date()
        if (query.start and day < query.start) or (query.end and day > query.end):
            return False
    if query.folder is not None and not in_folder(doc.folder, query.folder):
        return False
    if query.has_attachments is not None and bool(doc.attachments) != query.has_attachments:
        return False
    return True


def result_order(doc: MailDoc) -> tuple[int, float, str]:
    """检索结果的排序键：发送时间从新到旧，没有发送时间的排在最后，同一时间按 mail_id 升序。"""
    if doc.sent_at is None:
        return (1, 0.0, doc.mail_id)
    return (0, -datetime.fromisoformat(doc.sent_at).timestamp(), doc.mail_id)
