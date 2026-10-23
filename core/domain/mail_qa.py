"""邮件问答与会话线索的纯逻辑（批次 17b）。规格见 docs/spec-31-mail-qa.md。

- 相关度检索的“问题词元”（与 ES 的二元切分一致）、正文片段截取；
- 邮件资料的格式与提示词（答案与引文校验沿用 core.domain.qa.parse_answer）；
- 会话线索的根。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from core.domain.mail import CHINA, Address, MailDoc, fold, mail_tokens
from core.domain.qa import Passage

EXCERPT_CHARS = 1200            # 每封邮件交给模型与重排的正文片段上限
EXCERPT_LEAD = 100              # 片段从第一个命中词元之前最多这么多字开始
RECIPIENTS_LISTED = 10          # 资料中列出的收件人数
NO_MAIL_ANSWER = "邮件中暂未找到相关内容。"

MAIL_SYSTEM_PROMPT = (
    "你是企业知识库助手“小安”。现在只根据用户给出的邮件回答问题，不得使用邮件以外的知识，不得编造。\n"
    "邮件按 [编号] 给出，每封包含主题、发件人、收件人、时间和正文片段。输出要求：\n"
    "1. answer：简洁直接地回答，涉及人、时间、数字时写明；\n"
    "2. citations：支撑答案的引用列表，每项给出邮件编号 ref 和从该邮件资料中逐字摘抄的原文 quote"
    "（6 到 80 个字，必须与资料原文完全一致，不得改写、不得拼接）；\n"
    "3. 邮件不足以回答时，insufficient 为 true，answer 说明缺少什么信息，citations 可以为空；否则 insufficient 为 false。\n"
    "ref 必须是整数（邮件编号，不加引号）。只输出一个 JSON 对象，不要输出其他内容，格式示例：\n"
    '{"answer": "……", "insufficient": false, "citations": [{"ref": 1, "quote": "从邮件 1 中逐字摘抄的原文"}]}'
)


def question_keys(text: str) -> list[str]:
    """问题（或任意文本）的检索词元，与 ES 的 haina_bigram 分析一致：
    在 mail_tokens 的结果上，相邻且紧挨着的两个中日韩字符组成一个二元词；不与其他中日韩字符相邻的单个字符保留为单字；
    字母数字词元原样保留。去重，保持首次出现的顺序。"""
    tokens = mail_tokens(text)
    keys: list[str] = []
    for index, token in enumerate(tokens):
        if not token.cjk:
            keys.append(token.text)
            continue
        after = tokens[index + 1] if index + 1 < len(tokens) else None
        before = tokens[index - 1] if index > 0 else None
        joined_after = after is not None and after.cjk and after.start == token.end
        joined_before = before is not None and before.cjk and before.end == token.start
        if joined_after:
            keys.append(token.text + after.text)
        elif not joined_before:
            keys.append(token.text)
    return list(dict.fromkeys(keys))


def key_set(texts: Iterable[str]) -> set[str]:
    return {key for text in texts for key in question_keys(text)}


def doc_texts(doc: MailDoc) -> list[str]:
    """参与相关度检索的文本：主题、正文、附件名、发件人与收件人的显示名（与关键词检索的字段相同）。"""
    people = ([doc.sender] if doc.sender else []) + list(doc.to + doc.cc + doc.bcc)
    return [doc.subject, doc.body, *(item.name for item in doc.attachments), *(person.name for person in people)]


def relevance(doc: MailDoc, keys: Sequence[str]) -> int:
    """内存实现的相关度：doc 的检索文本中出现的不同问题词元个数。"""
    present = key_set(doc_texts(doc))
    return sum(key in present for key in dict.fromkeys(keys))


def excerpt(body: str, keys: Sequence[str], *, limit: int = EXCERPT_CHARS) -> str:
    """正文中与问题最相关的一段（body 的连续子串，长度不超过 limit）。

    - len(body) ≤ limit → 整个 body。
    - 否则：候选起点为 0，以及每个问题词元在 fold(body) 中每一处出现的位置减去 EXCERPT_LEAD（不小于 0）；
      fold 改变了长度时（NFKC 合并字符）只用 0。对每个候选起点 s，计算 body[s:s+limit] 中出现的不同问题词元个数
      （用 question_keys(片段) 判定，与检索一致，跨标点的两个汉字不算二元词），取最多的；并列时取最小的 s。"""
    if len(body) <= limit:
        return body
    wanted = list(dict.fromkeys(keys))
    folded = fold(body)
    starts = {0}
    for key in wanted if len(folded) == len(body) else []:
        position = folded.find(key)
        while position >= 0:
            starts.add(max(0, position - EXCERPT_LEAD))
            position = folded.find(key, position + 1)
    best, best_score = 0, -1
    for start in sorted(starts):
        present = set(question_keys(body[start:start + limit]))
        score = sum(key in present for key in wanted)
        if score > best_score:
            best, best_score = start, score
    return body[best:best + limit]


def _person(address: Address | None) -> str:
    if address is None:
        return "未知"
    if address.name and address.addr:
        return f"{address.name} <{address.addr}>"
    return address.name or address.addr or "未知"


def _time(sent_at: str | None) -> str:
    if not sent_at:
        return "未知"
    return datetime.fromisoformat(sent_at).astimezone(CHINA).strftime("%Y-%m-%d %H:%M")


def passage_text(doc: MailDoc, body_excerpt: str) -> str:
    """一封邮件交给模型的资料文本（逐字）：
        主题：<subject，空则“（无主题）”>
        发件人：<名字 <地址>，只有其一时为其一，没有为“未知”>
        收件人：<To 与 Cc 的前 RECIPIENTS_LISTED 个，用“、”连接；超过时末尾加“等 N 人”（N 为总数）；没有为“无”>
        时间：<UTC+08:00 的 YYYY-MM-DD HH:MM，没有为“未知”>
        正文：
        <body_excerpt>"""
    people = list(doc.to + doc.cc)
    listed = "、".join(_person(person) for person in people[:RECIPIENTS_LISTED]) or "无"
    if len(people) > RECIPIENTS_LISTED:
        listed += f"等 {len(people)} 人"
    return (f"主题：{doc.subject or '（无主题）'}\n发件人：{_person(doc.sender)}\n收件人：{listed}\n"
            f"时间：{_time(doc.sent_at)}\n正文：\n{body_excerpt}")


def mail_passages(docs: Sequence[MailDoc], keys: Sequence[str]) -> list[Passage]:
    """编号 1..n 的资料：source 为主题，section 与页码为 None，text 为 passage_text(doc, excerpt(doc.body, keys))。"""
    return [Passage(ref, doc.subject or "（无主题）", None, None, None, passage_text(doc, excerpt(doc.body, keys)))
            for ref, doc in enumerate(docs, 1)]


def build_mail_messages(question: str, passages: Sequence[Passage]) -> list[dict[str, str]]:
    """[{"role": "system", "content": MAIL_SYSTEM_PROMPT}, {"role": "user", "content": ...}]。
    user 内容（逐字）："邮件：\\n" + 各条 "[<ref>]\\n<text>" 用空行连接 + "\\n\\n问题：<question>"。
    question 为空白、passages 为空或编号不是 1..n → ValueError。"""
    question = question.strip()
    if not question or not passages or any(p.ref != i for i, p in enumerate(passages, 1)):
        raise ValueError("问题和邮件不能为空，编号须从 1 连续递增")
    blocks = [f"[{p.ref}]\n{p.text}" for p in passages]
    return [{"role": "system", "content": MAIL_SYSTEM_PROMPT},
            {"role": "user", "content": "邮件：\n" + "\n\n".join(blocks) + f"\n\n问题：{question}"}]


@dataclass(frozen=True)
class ThreadKey:
    """会话线索的查找条件：root 为会话根的 Message-ID；ids 为当前邮件引用过的全部 Message-ID（含 root 与自身）。"""
    root: str
    ids: tuple[str, ...]


def thread_key(doc: MailDoc) -> ThreadKey | None:
    """root：references 的第一个；没有 references 时为 in_reply_to；都没有时为自身的 message_id。
    ids：root、references、in_reply_to、message_id 去重后按此顺序。doc 没有任何 Message-ID 类信息 → None。"""
    root = (doc.references[0] if doc.references else None) or doc.in_reply_to or doc.message_id
    if root is None:
        return None
    ids = [root, *doc.references, *([doc.in_reply_to] if doc.in_reply_to else []),
           *([doc.message_id] if doc.message_id else [])]
    return ThreadKey(root, tuple(dict.fromkeys(ids)))


def in_thread(doc: MailDoc, key: ThreadKey) -> bool:
    """参照语义：doc 属于该会话 ⇔ doc.message_id ∈ key.ids，或 key.root ∈ doc.references，或 doc.in_reply_to == key.root。"""
    return ((doc.message_id is not None and doc.message_id in key.ids) or key.root in doc.references
            or doc.in_reply_to == key.root)


def thread_order(doc: MailDoc) -> tuple[int, float, str]:
    """会话线索的排序键：发送时间从早到晚，没有发送时间的排在最后，同一时间按 mail_id 升序。"""
    if doc.sent_at is None:
        return (1, 0.0, doc.mail_id)
    return (0, datetime.fromisoformat(doc.sent_at).timestamp(), doc.mail_id)
