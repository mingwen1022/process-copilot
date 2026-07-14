"""知识文档的 Markdown 分块（模块3 · Phase 2）。

政策文档是带小节标题（##/###）和条款的 markdown，按标题切块比按字数盲切更好：
每块天然对齐到一个"子句/小节"，正好当检索单元 + 引用定位（clause）。不引外部
splitter 依赖，自己做一个标题感知的切分。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DocChunk:
    text: str
    clause: str  # 该块所属的小节标题路径，如 "二、审批层级"
    index: int


def split_markdown(body: str, *, max_chars: int = 900) -> list[DocChunk]:
    """按 ## / ### 标题切块；超长块再按段落二次切。clause 取最近的标题。"""
    lines = body.splitlines()
    chunks: list[DocChunk] = []
    current_heading = ""
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        buffer.clear()
        if not text:
            return
        for piece in _split_long(text, max_chars):
            chunks.append(DocChunk(text=piece, clause=current_heading or "（正文）", index=len(chunks)))

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            flush()
            current_heading = stripped.lstrip("# ").strip()
        elif stripped.startswith("# "):
            # 一级标题（文档标题）不单独成块，作为上下文丢弃
            continue
        else:
            buffer.append(line)
    flush()
    return chunks


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for para in text.split("\n\n"):
        if size + len(para) > max_chars and current:
            parts.append("\n\n".join(current))
            current = []
            size = 0
        current.append(para)
        size += len(para) + 2
    if current:
        parts.append("\n\n".join(current))
    return parts
