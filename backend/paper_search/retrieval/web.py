"""Read publication pages directly; metadata is useful when present, never required."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import date
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from paper_search.domain import (
    Affiliation,
    Paper,
    PaperResolution,
    SourceContent,
    SourceRecord,
)
from paper_search.retrieval.code_links import repository_urls
from paper_search.retrieval.identity import (
    normalize_arxiv_id,
    normalize_doi,
    normalize_url,
)


def paper_from_card(card: dict) -> Paper | None:
    title, raw_url = card.get("title"), card.get("url")
    url = normalize_url(raw_url)
    if not isinstance(title, str) or not title.strip() or not url:
        return None
    doi = normalize_doi(raw_url)
    arxiv_id = normalize_arxiv_id(raw_url)
    raw_date = card.get("published_date")
    publication_date = None
    if isinstance(raw_date, str):
        match = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw_date)
        try:
            publication_date = (
                date(*(int(value) for value in match.groups())) if match else None
            )
        except ValueError:
            publication_date = None
    year_match = re.match(r"(\d{4})", raw_date) if isinstance(raw_date, str) else None
    year = (
        publication_date.year
        if publication_date
        else (int(year_match.group(1)) if year_match else None)
    )
    raw_authors = card.get("authors") or []
    if isinstance(raw_authors, str):
        raw_authors = [raw_authors]
    authors = []
    for item in raw_authors:
        value = (
            next(
                (
                    item.get(key)
                    for key in ("name", "full_name", "author")
                    if isinstance(item.get(key), str)
                ),
                "",
            )
            if isinstance(item, dict)
            else str(item)
        )
        if value.strip():
            authors.append(value.strip())
    canonical_id = (
        f"doi:{doi}" if doi else f"arxiv:{arxiv_id}" if arxiv_id else f"url:{url}"
    )
    return Paper(
        canonical_id=canonical_id,
        doi=doi,
        arxiv_id=arxiv_id,
        aliases=(raw_url,),
        title=title.strip(),
        authors=tuple(authors),
        year=year,
        publication_date=publication_date,
        venue=card.get("venue") if isinstance(card.get("venue"), str) else None,
        # Search-engine snippets are discovery hints assembled outside the
        # publication page.  They must never become citable paper abstracts.
        abstract=None,
        landing_page_url=url,
        source_urls=(url,),
        provider="web",
        source_records=(
            SourceRecord(
                provider="web",
                provider_paper_id=url,
                canonical_id=canonical_id,
                source_url=url,
                publication_date=publication_date,
            ),
        ),
    )


def page_text(page: BeautifulSoup) -> str:
    for node in page.select("script, style, nav, footer, noscript"):
        node.decompose()
    body = page.select_one("main, article") or page.body or page
    return body.get_text("\n", strip=True)


class WebPaperResolver:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.slots = asyncio.Semaphore(4)
        self.cache: dict[str, tuple[float, PaperResolution]] = {}

    def accepts(self, identifier: str) -> bool:
        return bool(normalize_doi(identifier)) or urlparse(identifier).scheme in {
            "http",
            "https",
        }

    async def resolve(self, identifier: str) -> PaperResolution:
        cached = self.cache.get(identifier)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        doi = normalize_doi(identifier)
        url = "https://doi.org/" + doi if doi else identifier
        try:
            async with self.slots:
                response = await self.client.get(url)
                response.raise_for_status()
            if "application/pdf" in response.headers.get("content-type", ""):
                return PaperResolution(
                    identifier=identifier,
                    status="unavailable",
                    reason="这是 PDF；使用 download_pdf 阅读，或读取论文摘要页面。",
                )
            page = BeautifulSoup(response.text, "html.parser")
            metadata: dict[str, list[str]] = {}
            for node in page.select("meta[content]"):
                key = str(node.get("name") or node.get("property") or "").casefold()
                value = str(node["content"]).strip()
                if value:
                    metadata.setdefault(key, []).append(value)

            def meta(*keys):
                return next((metadata[key][0] for key in keys if key in metadata), None)

            scholarly_title = meta("citation_title", "dc.title")
            title = scholarly_title or meta("og:title")
            if not title and page.title:
                title = page.title.get_text(" ", strip=True)
            text = page_text(page)
            if not title or not text:
                raise ValueError(
                    "页面没有可读标题或正文；可换网页抓取工具或原始论文链接"
                )
            published = meta("citation_publication_date", "citation_date", "dc.date")
            match = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", published or "")
            try:
                publication_date = (
                    date(*(int(v) for v in match.groups())) if match else None
                )
            except ValueError:
                publication_date = None
            year = re.match(r"\d{4}", published or "")
            doi = normalize_doi(meta("citation_doi", "dc.identifier"))
            landing = str(response.url)
            authors = tuple(
                metadata.get("citation_author", metadata.get("dc.creator", []))
            )
            exact_doi = bool(
                normalize_doi(identifier) and doi and normalize_doi(identifier) == doi
            )
            if normalize_doi(identifier) and not exact_doi:
                return PaperResolution(
                    identifier=identifier,
                    status="unresolved",
                    reason="页面未声明请求的 DOI，不能把请求值作为论文身份或别名",
                    source=SourceContent(url=landing, text=text),
                )
            identity_verified = (
                exact_doi
                if normalize_doi(identifier)
                else bool(scholarly_title and authors)
            )
            paper = Paper(
                canonical_id="doi:" + doi
                if doi
                else "url:" + (normalize_url(landing) or landing),
                aliases=tuple(dict.fromkeys((identifier, landing))),
                title=title,
                authors=authors,
                year=publication_date.year
                if publication_date
                else int(year[0])
                if year
                else None,
                publication_date=publication_date,
                doi=doi,
                venue=meta("citation_conference_title", "citation_journal_title"),
                affiliations=tuple(
                    Affiliation(name=value, provider="web", source_url=landing)
                    for value in metadata.get("citation_author_institution", [])
                ),
                # Generic description/og:description fields frequently contain
                # search or marketing copy.  Only explicit scholarly abstract
                # metadata is promoted to formal abstract evidence.
                abstract=meta("citation_abstract"),
                landing_page_url=landing,
                pdf_url=urljoin(landing, meta("citation_pdf_url"))
                if meta("citation_pdf_url")
                else None,
                source_urls=(landing,),
                provider="web",
                source_records=(
                    SourceRecord(
                        provider="web",
                        provider_paper_id=doi or landing,
                        canonical_id=("doi:" + doi)
                        if doi
                        else "url:" + (normalize_url(landing) or landing),
                        source_url=landing,
                        publication_date=publication_date,
                        identity_status="verified"
                        if identity_verified
                        else "discovered",
                    ),
                ),
            )
            paper = paper.model_copy(
                update={"code_urls": repository_urls(paper.abstract)}
            )
            result = PaperResolution(
                identifier=identifier,
                status="resolved",
                paper=paper,
                reason=None
                if identity_verified
                else "页面可读，但身份来源仍为 discovered；正式比较前需要原始论文元数据核验",
                source=SourceContent(url=landing, text=text),
            )
            self.cache[identifier] = (time.monotonic(), result)
            return result
        except (httpx.HTTPError, ValueError) as exc:
            return PaperResolution(
                identifier=identifier, status="unavailable", reason=str(exc)[:300]
            )
