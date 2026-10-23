"""Web 与 worker 使用的运行常量及文件命名函数。由原项目现有实现原样抽取。"""

from __future__ import annotations

import re

CONFIG_HASH = "docling-serve-1.37.0-lo.v1"

PARSER_VERSION = "docling-serve 1.37.0"

DEFAULT_CHAT_MODEL = "qwen3.6-27b"

ANSWER_FILE = "_RAG测试资料说明.md"

TITLE_PREFIXES = ("简单", "复杂", "长文档", "扫描件", "图片", "旧版", "其他")

def title_for(filename: str) -> str:
    """文档标题：去掉目录和扩展名；若以 TITLE_PREFIXES 之一加 "_" 开头且去掉后仍非空，去掉该前缀
    （"复杂_.pdf" → "复杂_"）。
    例："复杂_ES-3000产品技术规格书.docx" → "ES-3000产品技术规格书"；"report.final.pdf" → "report.final"。"""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    title = name.rsplit(".", 1)[0] if "." in name else name
    for prefix in TITLE_PREFIXES:
        if title.startswith(prefix + "_") and len(title) > len(prefix) + 1:
            return title[len(prefix) + 1:]
    return title

def version_id_for(index: int, sha256_hex: str) -> str:
    """版本 ID："v" + 两位以上序号 + "_" + 原件 sha256 前 10 位（小写十六进制）。
    index 为负、sha256_hex 不是 64 位十六进制 → ValueError。例：(3, "ab…") → "v03_ab12cd34ef"。"""
    if index < 0 or re.fullmatch(r"[0-9a-fA-F]{64}", sha256_hex) is None:
        raise ValueError("序号须非负，哈希须为 64 位十六进制")
    return f"v{index:02d}_{sha256_hex[:10].lower()}"
