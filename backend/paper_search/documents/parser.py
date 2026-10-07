from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

from paper_search.domain import ParsedDocument

logger = logging.getLogger(__name__)


class PdfParser:
    """A cancellable process boundary; PDF parsing never blocks the API loop."""

    def __init__(self, *, timeout: float = 300, max_pages: int = 200):
        self.timeout, self.max_pages = timeout, max_pages

    async def parse(self, path: str) -> ParsedDocument:
        with tempfile.TemporaryDirectory(prefix="paper-search-pdf-") as directory:
            output = Path(directory) / "document.json"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "paper_search.documents.worker",
                path,
                str(output),
                str(self.max_pages),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                creationflags=0x08000000 if sys.platform == "win32" else 0,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            try:
                async with asyncio.timeout(self.timeout):
                    _, error = await process.communicate()
                if process.returncode:
                    details = error.decode("utf-8", errors="replace").strip()
                    logger.error(
                        "PDF parser exited with code %s: %s",
                        process.returncode,
                        details,
                    )
                    details = (
                        details.splitlines()[-1][:500]
                        if details
                        else "解析进程未返回原因"
                    )
                    raise ValueError(f"PDF 解析失败：{details}")
                return ParsedDocument.model_validate_json(
                    output.read_text(encoding="utf-8")
                )
            except TimeoutError:
                raise TimeoutError(
                    f"PDF 解析超过 {self.timeout:g} 秒，解析进程已终止"
                ) from None
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.communicate()
