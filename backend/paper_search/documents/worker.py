"""PDF parser process entry point. Only validated project values cross the boundary."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from paper_search.domain import DocumentBlock, ParsedDocument


def split_page(text: str, *, max_chars: int = 1200) -> list[str]:
    """Coalesce PDF text lines into bounded retrieval and citation units."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for word in text.replace("\x00", "").split():
        if len(word) > max_chars:
            if current:
                chunks.append(" ".join(current))
                current, size = [], 0
            chunks.extend(
                word[start : start + max_chars]
                for start in range(0, len(word), max_chars)
            )
            continue
        extra = len(word) + (1 if current else 0)
        if current and size + extra > max_chars:
            chunks.append(" ".join(current))
            current, size = [], 0
            extra = len(word)
        current.append(word)
        size += extra
        if size == max_chars:
            chunks.append(" ".join(current))
            current, size = [], 0
    if current:
        chunks.append(" ".join(current))
    return chunks


def convert(path: str, max_pages: int) -> ParsedDocument:
    from pypdf import PdfReader

    reader = PdfReader(path)
    page_count = min(len(reader.pages), max_pages)
    heading = re.compile(
        r"^(?:\d+(?:\.\d+)*\.?\s*)?(Abstract|Introduction|Related Work|Background|Methods?|Methodology|Approach|Experiments?|Evaluation|Results|Discussion|Limitations|Conclusions?|References|Appendix)\s*[:.]?$",
        re.IGNORECASE,
    )
    blocks, section = [], ""
    for page_number, page in enumerate(reader.pages[:page_count], 1):
        lines = []
        for line in (page.extract_text() or "").splitlines():
            if heading.fullmatch(line.strip()):
                blocks.extend(
                    DocumentBlock(page=page_number, section=section, text=chunk)
                    for chunk in split_page("\n".join(lines))
                )
                lines = []
                section = line.strip()
            lines.append(line)
        blocks.extend(
            DocumentBlock(page=page_number, section=section, text=chunk)
            for chunk in split_page("\n".join(lines))
        )
    if sum(len(block.text) for block in blocks) < 80:
        raise ValueError("文本不足：可能是扫描件或不可提取文本的 PDF；本版本未启用 OCR")
    return ParsedDocument(blocks=tuple(blocks), page_count=page_count, parser="pypdf")


def main():
    source, output, max_pages = sys.argv[1:]
    Path(output).write_text(
        convert(source, int(max_pages)).model_dump_json(), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
