"""邮件（批次 17a）：.eml 解析、检索用的分词与匹配规则、邮件记录。规格见 docs/spec-30-mail.md。

纯逻辑：只用标准库的 email 包解析内存中的字节，不读文件、不读时钟。
"""
from __future__ import annotations

import hashlib
import base64
import binascii
import quopri
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

MAIL_EXTENSIONS = frozenset({"eml"})
MAX_MAIL_BYTES = 100_000_000        # 超过的文件不读取（扫描记为跳过）
MAX_BODY_CHARS = 200_000            # 正文超过时截断，body_truncated 为真
MAX_SUBJECT_CHARS = 1_000
MAX_ADDRESSES = 500                 # 每个收件人字段最多保留的地址数
MAX_REFERENCES = 50
MAX_ATTACHMENTS = 200
CHINA = timezone(timedelta(hours=8))

# 解析中发现的问题（ParsedMail.problems 的取值，按此顺序排列、不重复）
CHARSET_FALLBACK = "charset_fallback"   # 声明的字符集缺失、未知或解码失败，改用其他字符集或替换字符
HEADER_DECODE = "header_decode"         # 某个头字段的编码字无法按声明解码
BAD_DATE = "bad_date"                   # Date 头存在但无法解析，或年份不在 1970–2100
DATE_NO_ZONE = "date_no_zone"           # Date 没有时区（含 -0000），按 UTC+08:00 处理
NO_BODY = "no_body"                     # 没有可用的正文（纯文本与 HTML 都没有）
TRUNCATED = "truncated"                 # 正文、主题、地址、引用或附件清单被截断
DEFECTS = "defects"                     # email 包报告了结构缺陷（例如 multipart 边界缺失）
PROBLEMS = (CHARSET_FALLBACK, HEADER_DECODE, BAD_DATE, DATE_NO_ZONE, NO_BODY, TRUNCATED, DEFECTS)

# 字符集别名：旧版中文邮件常把 GBK/GB18030 的内容标成 gb2312
@dataclass(frozen=True)
class Address:
    """name：显示名（已解码，去首尾空白，可为 ""）；addr：小写的邮箱地址（无法识别时为 ""）。"""
    name: str
    addr: str

    def to_json(self) -> dict[str, str]:
        return {"name": self.name, "addr": self.addr}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Address:
        return cls(data["name"], data["addr"])


@dataclass(frozen=True)
class Attachment:
    """附件的元数据（批次 17a 不收录附件内容）。size：解码后的字节数。"""
    name: str
    content_type: str
    size: int

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "content_type": self.content_type, "size": self.size}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Attachment:
        return cls(data["name"], data["content_type"], data["size"])


@dataclass(frozen=True)
class ParsedMail:
    message_id: str | None              # 去掉尖括号与空白；没有为 None
    subject: str                        # 已解码；超过 MAX_SUBJECT_CHARS 截断
    sender: Address | None              # From 的第一个地址
    to: tuple[Address, ...]
    cc: tuple[Address, ...]
    bcc: tuple[Address, ...]
    sent_at: datetime | None            # 带时区，已换算到 UTC+08:00
    in_reply_to: str | None
    references: tuple[str, ...]
    body: str
    body_truncated: bool
    body_kind: str                      # "plain"、"html" 或 "none"
    attachments: tuple[Attachment, ...]
    problems: tuple[str, ...]


@dataclass(frozen=True)
class MailDoc:
    """一封邮件在检索索引中的记录。一个文件一条记录；同一封邮件被导出到多个文件夹时是多条记录。

    mail_id = mail_id(root, path)；folder = join_folder(root, path 的目录部分)；sent_at 为 UTC+08:00 的 isoformat 或 None。"""
    mail_id: str
    root: str
    path: str
    folder: str
    size: int
    message_id: str | None
    subject: str
    sender: Address | None
    to: tuple[Address, ...]
    cc: tuple[Address, ...]
    bcc: tuple[Address, ...]
    sent_at: str | None
    in_reply_to: str | None
    references: tuple[str, ...]
    body: str
    body_truncated: bool
    body_kind: str
    attachments: tuple[Attachment, ...]
    problems: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {"mail_id": self.mail_id, "root": self.root, "path": self.path, "folder": self.folder, "size": self.size,
                "message_id": self.message_id, "subject": self.subject,
                "sender": self.sender.to_json() if self.sender else None,
                "to": [a.to_json() for a in self.to], "cc": [a.to_json() for a in self.cc],
                "bcc": [a.to_json() for a in self.bcc], "sent_at": self.sent_at, "in_reply_to": self.in_reply_to,
                "references": list(self.references), "body": self.body, "body_truncated": self.body_truncated,
                "body_kind": self.body_kind, "attachments": [a.to_json() for a in self.attachments],
                "problems": list(self.problems)}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> MailDoc:
        return cls(data["mail_id"], data["root"], data["path"], data["folder"], data["size"], data["message_id"],
                   data["subject"], Address.from_json(data["sender"]) if data["sender"] else None,
                   tuple(map(Address.from_json, data["to"])), tuple(map(Address.from_json, data["cc"])),
                   tuple(map(Address.from_json, data["bcc"])), data["sent_at"], data["in_reply_to"],
                   tuple(data["references"]), data["body"], data["body_truncated"], data["body_kind"],
                   tuple(map(Attachment.from_json, data["attachments"])), tuple(data["problems"]))


def mail_id(root: str, path: str) -> str:
    """邮件记录的 ID：sha256(f"{root}\\0{path}") 的前 32 位十六进制。同一位置的文件内容变了，ID 不变（整体替换记录）。"""
    return hashlib.sha256(f"{root}\0{path}".encode("utf-8")).hexdigest()[:32]


def build_doc(root: str, path: str, folder: str, size: int, parsed: ParsedMail) -> MailDoc:
    return MailDoc(mail_id(root, path), root, path, folder, size, parsed.message_id, parsed.subject, parsed.sender,
                   parsed.to, parsed.cc, parsed.bcc, parsed.sent_at.isoformat() if parsed.sent_at else None,
                   parsed.in_reply_to, parsed.references, parsed.body, parsed.body_truncated, parsed.body_kind,
                   parsed.attachments, parsed.problems)


# ---------------------------------------------------------------- 解析

class NotMail(ValueError):
    """内容不是邮件：开头找不到任何头字段（"名称: 值" 形式的行），或内容为空。"""


def parse_eml(data: bytes) -> ParsedMail:
    """解析一封 RFC 5322 / MIME 邮件。除 NotMail 外不抛异常：问题记在 problems 里，尽量给出可用的结果。

    前置检查：去掉开头的空白行与 mbox 的 "From " 行之后，第一行必须是头字段（名称为可打印 ASCII，不含冒号），
    否则 NotMail。

    头字段（用 compat32 解析，原始头允许 8 位字节）：
    - 每个值先按 decode_header_text 解码（RFC 2047 编码字与未编码的 8 位字节都处理），折行合并为一个空格，去首尾空白。
    - message_id、in_reply_to：取第一个 <...>，去掉尖括号；没有尖括号时取去空白后的整个值；空则 None。
    - references：References 中全部 <...> 的内容，按出现顺序去重，最多 MAX_REFERENCES 个（超出 → TRUNCATED）。
    - 地址：From、To、Cc、Bcc 先在原始值上用 getaddresses 拆分，再分别解码显示名；addr 小写去空白；
      addr 与 name 都为空的项丢弃；每个字段最多 MAX_ADDRESSES 个（超出 → TRUNCATED）。sender 为 From 的第一项或 None。
    - subject：超过 MAX_SUBJECT_CHARS 截断（TRUNCATED）。
    - sent_at：parsedate_to_datetime(Date)；失败或年份不在 1970–2100 → None 并记 BAD_DATE；
      结果没有时区（含 -0000）→ 视为 UTC+08:00 并记 DATE_NO_ZONE；最后换算到 UTC+08:00。没有 Date 头 → None，不记问题。

    正文与附件（遍历 MIME 树）：
    - 附件：Content-Disposition 为 attachment 的部分、带文件名（filename 或 name 参数）的非 multipart 部分、
      以及 message/rfc822 部分（整体算一个附件，不再进入其内部）。名称为解码后的文件名；
      message/rfc822 没有文件名时用其主题加 ".eml"，主题也没有时为 "邮件.eml"；其他没有名称的为 ""。
      content_type 为小写的类型；size 为解码后内容的字节数。最多 MAX_ATTACHMENTS 个（超出 → TRUNCATED）。
    - 正文候选：其余的 text/plain 与 text/html 部分。multipart/alternative 内只取一个：有 text/plain 取第一个
      text/plain，否则取第一个 text/html（不论在 alternative 的哪一层嵌套中）。
      其他位置的候选都按出现顺序收入。
    - 有任一 text/plain 被收入 → body_kind "plain"，正文为各 text/plain 候选的文本，用 "\\n\\n" 连接（HTML 候选丢弃）；
      否则有 text/html → "html"，正文为各 HTML 候选的 html_to_text 结果，用 "\\n\\n" 连接；都没有 → "none"、正文 ""，记 NO_BODY。
    - 每个部分的文本用 decode_bytes(内容, 声明的字符集) 解码。
    - 最后 normalize_text；超过 MAX_BODY_CHARS 截断并置 body_truncated（同时记 TRUNCATED）。
    - email 包在任一部分报告了 defects → 记 DEFECTS。
    problems 按 PROBLEMS 的顺序排列、不重复。"""
    lines = data.splitlines(keepends=True)
    start = 0
    while start < len(lines) and (not lines[start].strip() or lines[start].startswith(b"From ")):
        start += 1
    if start == len(lines) or re.match(rb"[\x21-\x39\x3b-\x7e]+:", lines[start]) is None:
        raise NotMail("开头没有邮件头字段")
    source = b"".join(lines[start:])
    problems: set[str] = set()
    try:
        message = BytesParser(policy=policy.compat32).parsebytes(source)
    except Exception:
        # 极端损坏的 MIME 树仍保留可以独立解析的头字段。
        message = BytesParser(policy=policy.compat32).parsebytes(source, headersonly=True)
        problems.add(DEFECTS)

    def header(part: Message, name: str) -> str:
        values = []
        for raw in _mail_headers(part, name):
            value, issue = decode_header_text(raw)
            if issue:
                problems.add(issue)
            values.append(value)
        return " ".join(values)

    def addresses(name: str) -> tuple[Address, ...]:
        result = []
        for raw_name, addr in getaddresses(_mail_headers(message, name)):
            name_value, issue = decode_header_text(raw_name)
            if issue:
                problems.add(issue)
            addr = _mail_utf8(addr).strip().lower()
            if name_value or addr:
                result.append(Address(name_value, addr))
        if len(result) > MAX_ADDRESSES:
            problems.add(TRUNCATED)
        return tuple(result[:MAX_ADDRESSES])

    subject = header(message, "subject")
    if len(subject) > MAX_SUBJECT_CHARS:
        subject = subject[:MAX_SUBJECT_CHARS]
        problems.add(TRUNCATED)
    senders, to, cc, bcc = (addresses(name) for name in ("from", "to", "cc", "bcc"))
    references = tuple(dict.fromkeys(value.strip() for value in re.findall(r"<([^<>]*)>", header(message, "references"))
                                    if value.strip()))
    if len(references) > MAX_REFERENCES:
        references = references[:MAX_REFERENCES]
        problems.add(TRUNCATED)
    sent_at = None
    if _mail_headers(message, "date"):
        try:
            sent_at = parsedate_to_datetime(header(message, "date"))
            if not 1970 <= sent_at.year <= 2100:
                raise ValueError("日期年份越界")
            if sent_at.tzinfo is None or sent_at.utcoffset() is None:
                sent_at = sent_at.replace(tzinfo=CHINA)
                problems.add(DATE_NO_ZONE)
            sent_at = sent_at.astimezone(CHINA)
        except (ValueError, TypeError, OverflowError, IndexError):
            sent_at = None
            problems.add(BAD_DATE)

    attachments: list[Attachment] = []
    visited: list[Message] = []
    raw_payloads: dict[int, bytes] = {}
    line_policy = policy.compat32.clone(linesep="\r\n" if b"\r\n" in source else "\n", max_line_length=0)

    def filename(part: Message) -> str | None:
        decoded = Message(policy=policy.compat32)
        for key in ("content-disposition", "content-type"):
            value = header(part, key)
            if value:
                decoded[key] = value
        for key, parameter in (("content-disposition", "filename"), ("content-type", "name")):
            value = decoded.get_param(parameter, header=key)
            if value is None:
                continue
            if isinstance(value, tuple):
                charset, _, text = value
                value, fallback = decode_bytes(text.encode("raw-unicode-escape"), charset)
                if fallback:
                    problems.add(CHARSET_FALLBACK)
            value, issue = decode_header_text(value)
            if issue:
                problems.add(issue)
            return value
        return None

    def payload(part: Message) -> bytes:
        try:
            if id(part) in raw_payloads:
                # 保留原始换行和 multipart 边界；重新序列化会改变混合换行的字节数。
                encoded = Message(policy=policy.compat32)
                encoded.set_payload(raw_payloads[id(part)].decode("ascii", "surrogateescape"))
                encoded["Content-Transfer-Encoding"] = part.get("Content-Transfer-Encoding", "8bit")
                value = encoded.get_payload(decode=True)
                if encoded.defects:
                    problems.add(DEFECTS)
                return value or b""
            value = part.get_payload(decode=True)
            if isinstance(value, bytes):
                return value
            raw = part.get_payload()
            if isinstance(raw, list):
                # multipart 附件的内容也包含前言和边界；message/rfc822 则包含内层邮件头。
                wire = part.as_bytes(policy=line_policy)
                return wire.partition(line_policy.linesep.encode("ascii") * 2)[2]
            return (raw or "").encode("utf-8", "surrogateescape")
        except (ValueError, TypeError, UnicodeError, LookupError):
            problems.add(DEFECTS)
            return b""

    def candidates(part: Message, wire: bytes | None = None) -> list[tuple[str, Message]]:
        visited.append(part)
        if wire is not None:
            raw_body = _mail_wire_body(wire)
            if raw_body is not None:
                raw_payloads[id(part)] = raw_body
        kind = part.get_content_type().lower()
        name = filename(part)
        if kind == "message/rfc822" or part.get_content_disposition() == "attachment" or (name is not None and not part.is_multipart()):
            if kind == "message/rfc822" and not name:
                inner = part.get_payload()
                name = ((header(inner[0], "subject") if isinstance(inner, list) and inner else "") or "邮件") + ".eml"
            if len(attachments) < MAX_ATTACHMENTS:
                attachments.append(Attachment(name or "", kind, len(payload(part))))
            else:
                problems.add(TRUNCATED)
            return []
        if part.is_multipart():
            children = part.get_payload()
            wires = _mail_wire_children(raw_payloads.get(id(part)), part.get_boundary())
            if len(wires) != len(children):
                wires = [None] * len(children)
            result = [item for child, child_wire in zip(children, wires) for item in candidates(child, child_wire)]
            if kind == "multipart/alternative":
                plain = [item for item in result if item[0] == "plain"]
                return (plain or result)[:1]
            return result
        return [(kind[5:], part)] if kind in ("text/plain", "text/html") else []

    try:
        parts = candidates(message, source)
    except RecursionError:
        parts = []
        problems.add(DEFECTS)
    plain = [part for kind, part in parts if kind == "plain"]
    selected = plain or [part for _, part in parts]
    body_kind = "plain" if plain else "html" if selected else "none"
    texts = []
    for part in selected:
        text, fallback = decode_bytes(payload(part), part.get_content_charset())
        if fallback:
            problems.add(CHARSET_FALLBACK)
        texts.append(html_to_text(text) if body_kind == "html" else text)
    body = normalize_text("\n\n".join(texts))
    if not selected:
        problems.add(NO_BODY)
    body_truncated = len(body) > MAX_BODY_CHARS
    if body_truncated:
        body = body[:MAX_BODY_CHARS]
        problems.add(TRUNCATED)
    if any(part.defects for part in visited):
        problems.add(DEFECTS)
    return ParsedMail(_mail_message_id(header(message, "message-id")), subject, senders[0] if senders else None,
                      to, cc, bcc, sent_at, _mail_message_id(header(message, "in-reply-to")), references,
                      body, body_truncated, body_kind, tuple(attachments), tuple(code for code in PROBLEMS if code in problems))


def decode_bytes(data: bytes, charset: str | None) -> tuple[str, bool]:
    """按声明的字符集解码，返回 (文本, 是否回退)。

    - charset 先小写去空白并按别名表映射（gb2312、gbk 等 → gb18030；ks_c_5601-1987 → cp949；big5 → big5hkscs；utf8 → utf-8）。
    - 声明的字符集存在且能严格解码 → (文本, False)。us-ascii 解码失败时按未声明处理（不算回退以外的错误）。
    - 否则依次严格尝试 utf-8、gb18030，成功 → (文本, True)；都失败 → utf-8 加替换字符，(文本, True)。
    - 未声明字符集（None 或空）且内容是纯 ASCII → (文本, False)；不是纯 ASCII 时按上一条，回退为 True。"""
    declared = (charset or "").strip().lower()
    aliases = {"gb2312": "gb18030", "gbk": "gb18030", "cp936": "gb18030",
               "ks_c_5601-1987": "cp949", "big5": "big5hkscs", "utf8": "utf-8"}
    declared = aliases.get(declared, declared)
    if declared:
        try:
            text = data.decode(declared, "strict")
            text.encode("utf-8", "strict")
            return text, False
        except (UnicodeError, LookupError, ValueError):
            pass
    elif data.isascii():
        return data.decode("ascii"), False
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding, "strict"), True
        except UnicodeError:
            pass
    return data.decode("utf-8", "replace"), True


def decode_header_text(raw: str) -> tuple[str, str | None]:
    """解码一个头字段值，返回 (文本, 问题代码或 None)。

    - raw 中未编码的 8 位字节（以 surrogateescape 形式保存）先还原为字节：能按 UTF-8 严格解码则照用（RFC 6532，不算问题）；
      否则按 decode_bytes(…, None) 解码并记 CHARSET_FALLBACK。
    - 再解码 RFC 2047 编码字 =?字符集?B|Q?内容?=（B 为 base64，缺少的填充补齐；Q 为 quoted-printable，"_" 为空格）：
      每个编码字按其声明的字符集 decode_bytes；内容无法解码或发生回退 → HEADER_DECODE。
      两个编码字之间只有空白时，空白去掉；其余文字原样保留。
    - 折行（CRLF/LF 后跟空白）与连续空白合并为一个空格，去首尾空白。
    两种问题都有时报 HEADER_DECODE。"""
    issue = None

    def restore(match: re.Match[str]) -> str:
        nonlocal issue
        value = match.group()
        if not any("\udc80" <= char <= "\udcff" for char in value):
            return value
        data = value.encode("utf-8", "surrogateescape")
        try:
            return data.decode("utf-8", "strict")
        except UnicodeError:
            issue = CHARSET_FALLBACK
            return decode_bytes(data, None)[0]

    raw = re.sub(r"[\x00-\x7f\udc80-\udcff]+", restore, raw)
    pattern = re.compile(r"=\?([^?\s]+)\?([bBqQ])\?([^?]*)\?=")
    parts: list[str] = []
    end = 0
    previous = False
    for match in pattern.finditer(raw):
        between = raw[end:match.start()]
        if not (previous and not between.strip()):
            parts.append(between)
        try:
            charset, encoding, value = match.groups()
            encoded = value.encode("ascii")
            if encoding.lower() == "b":
                encoded = re.sub(rb"\s+", b"", encoded)
                decoded = base64.b64decode(encoded + b"=" * (-len(encoded) % 4), validate=True)
            else:
                decoded = quopri.decodestring(encoded.replace(b"_", b" "))
            text, fallback = decode_bytes(decoded, charset)
            if fallback:
                issue = HEADER_DECODE
            parts.append(text)
        except (ValueError, UnicodeError, binascii.Error):
            issue = HEADER_DECODE
            parts.append(match.group())
        end = match.end()
        previous = True
    parts.append(raw[end:])
    return _mail_utf8(re.sub(r"\s+", " ", "".join(parts)).strip()), issue


# ---------------------------------------------------------------- HTML 与文本

def html_to_text(source: str) -> str:
    """HTML 转纯文本：去掉 script、style、head、title、noscript、template 的内容与全部标签（含注释）；
    块级元素（p、div、br、tr、li、h1–h6、table、ul、ol、blockquote、pre、hr 等）前后换行，td、th 前加空格；
    字符实体解码（&nbsp; 视为空格）；最后 normalize_text。解析出错时退化为去掉 <...> 后的 normalize_text。"""
    class _TextParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []
            self.hidden: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag in {"script", "style", "head", "title", "noscript", "template"}:
                self.hidden.append(tag)
            if self.hidden:
                return
            if tag in {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol",
                       "blockquote", "pre", "hr", "section", "article", "header", "footer", "main", "dl", "dt", "dd"}:
                self.parts.append("\n")
            elif tag in {"td", "th"}:
                self.parts.append(" ")

        def handle_endtag(self, tag: str) -> None:
            if self.hidden:
                if tag in self.hidden:
                    del self.hidden[self.hidden.index(tag):]
                return
            if tag not in {"td", "th", "br", "hr", "script", "style", "head", "title", "noscript", "template"}:
                self.handle_starttag(tag, [])

        def handle_data(self, data: str) -> None:
            if not self.hidden:
                self.parts.append(data)

    parser = _TextParser()
    try:
        parser.feed(source)
        parser.close()
        return normalize_text("".join(parser.parts))
    except Exception:
        return normalize_text(re.sub(r"<[^>]*>", "", source))


def normalize_text(text: str) -> str:
    """CRLF、CR → LF；去掉 NUL 等控制字符（保留 \\n 与 \\t，\\t 视为空格）；每行内连续空白合并为一个空格并去首尾空白；
    连续两个以上的空行合并为一个空行；去掉首尾空行。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(char for char in text if char in "\n\t" or unicodedata.category(char) != "Cc")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


def _mail_utf8(text: str) -> str:
    """还原未编码的头字段字节，非法 UTF-8 与孤立代理字符用替换字符表示。"""
    restored = re.sub(r"[\udc80-\udcff]+",
                      lambda match: match.group().encode("utf-8", "surrogateescape").decode("utf-8", "replace"), text)
    return re.sub(r"[\ud800-\udfff]", "\ufffd", restored)


def _mail_headers(part: Message, name: str) -> list[str]:
    """使用原始头值保留 compat32 的 surrogateescape 字节。"""
    return [value for key, value in part.raw_items() if key.lower() == name]


def _mail_message_id(value: str) -> str | None:
    """只取第一个尖括号内的标识；没有尖括号时保留完整值。"""
    found = re.search(r"<([^<>]*)>", value)
    return (found.group(1) if found else value).strip() or None


def _mail_wire_body(wire: bytes) -> bytes | None:
    """提取原始头之后的字节，不把正文中的 LF 规范化成 CRLF。"""
    for match in re.finditer(rb"[^\r\n]*(?:\r\n|\r|\n|$)", wire):
        if match.group() and not match.group().rstrip(b"\r\n"):
            return wire[match.end():]
    return None


def _mail_wire_children(body: bytes | None, boundary: str | None) -> list[bytes | None]:
    """将原始 MIME 段与 email 包的子节点对应；结构不符时由调用方使用解析结果回退。"""
    if body is None or not boundary:
        return []
    marker = re.escape(boundary.encode("utf-8", "surrogateescape"))
    delimiters = re.finditer(rb"(?:^|(?<=[\r\n]))--" + marker + rb"(--)?[ \t]*(?:\r\n|\n|\r|$)", body)
    result: list[bytes | None] = []
    start = None
    for match in delimiters:
        if start is not None:
            child = body[start:match.start()]
            # 紧邻边界的一个换行属于边界，不属于前一个部分。
            child = child[:-2] if child.endswith(b"\r\n") else child[:-1] if child.endswith((b"\r", b"\n")) else child
            result.append(child)
        if match.group(1):
            start = None
            break
        start = match.end()
    if start is not None:
        result.append(body[start:])
    return result


# ---------------------------------------------------------------- 检索的匹配规则（契约）

def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF
            or 0x20000 <= code <= 0x323AF or 0x3040 <= code <= 0x30FF or 0xAC00 <= code <= 0xD7AF)


@dataclass(frozen=True)
class Token:
    text: str
    cjk: bool
    start: int          # 在 fold(原文) 中的起止位置
    end: int


def fold(text: str) -> str:
    """匹配前的规范化：NFKC 后转小写。"""
    return unicodedata.normalize("NFKC", text).lower()


def mail_tokens(text: str) -> list[Token]:
    """检索分词（规格 30 第四节的契约，ES 的二元切分在这些情形下与之一致）：在 fold(text) 上，
    每个中日韩字符是一个词元（cjk=True）；连续的字母数字（str.isalnum 且不是中日韩字符）是一个词元；其余字符都是分隔符。"""
    folded = fold(text)
    tokens: list[Token] = []
    index = 0
    while index < len(folded):
        char = folded[index]
        if _is_cjk(char):
            tokens.append(Token(char, True, index, index + 1))
            index += 1
        elif char.isalnum():
            end = index
            while end < len(folded) and folded[end].isalnum() and not _is_cjk(folded[end]):
                end += 1
            tokens.append(Token(folded[index:end], False, index, end))
            index = end
        else:
            index += 1
    return tokens


def term_matches(term: str, text: str) -> bool:
    """关键词 term 是否出现在 text 中：term 的词元序列在 text 的词元序列中连续出现，
    且其中相邻的两个中日韩词元在 text 中也紧挨着（中间没有分隔符）。term 没有词元 → False。"""
    wanted = mail_tokens(term)
    if not wanted:
        return False
    tokens = mail_tokens(text)
    keys = [token.text for token in wanted]
    for start in range(len(tokens) - len(wanted) + 1):
        window = tokens[start:start + len(wanted)]
        if [token.text for token in window] != keys:
            continue
        if all(not (a.cjk and b.cjk) or a.end == b.start for a, b in zip(window, window[1:])):
            return True
    return False


def any_matches(term: str, texts: Iterable[str]) -> bool:
    return any(term_matches(term, text) for text in texts)


SNIPPET_CHARS = 200


def make_snippet(body: str, terms: Sequence[str]) -> str:
    """检索结果的摘要：body 的一段连续子串，长度不超过 SNIPPET_CHARS。
    有关键词出现在 body 中时，取第一个能在 body 中找到的关键词的首次出现处，向前最多 40 个字符起截取；
    否则取 body 开头。换行替换为空格之前截取（返回的仍是 body 的原样子串）。"""
    folded = fold(body)
    for term in terms:
        position = _find(term, body, folded)
        if position is not None:
            start = max(0, position - 40)
            return body[start:start + SNIPPET_CHARS]
    return body[:SNIPPET_CHARS]


def _find(term: str, body: str, folded: str) -> int | None:
    """term 在 body 中首次匹配的起点（按 fold 后的位置近似换算回原文；NFKC 改变长度时退化为 0）。"""
    wanted = mail_tokens(term)
    if not wanted:
        return None
    tokens = mail_tokens(body)
    keys = [token.text for token in wanted]
    for start in range(len(tokens) - len(wanted) + 1):
        window = tokens[start:start + len(wanted)]
        if [token.text for token in window] == keys and all(
                not (a.cjk and b.cjk) or a.end == b.start for a, b in zip(window, window[1:])):
            return window[0].start if len(folded) == len(body) else 0
    return None
