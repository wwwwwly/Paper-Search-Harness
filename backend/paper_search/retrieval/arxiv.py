from __future__ import annotations

import asyncio
import re
import time
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup

from paper_search.domain import Affiliation, Paper, PaperResolution, SourceRecord
from paper_search.retrieval.code_links import repository_urls
from paper_search.retrieval.identity import normalize_arxiv_id, normalize_doi


class ArxivProvider:
    name = "arxiv"

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str = "https://export.arxiv.org/api/query",
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._slot = asyncio.Lock()
        self._last_request = 0.0

    async def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        async with self._slot:
            await asyncio.sleep(max(0, 3 - (time.monotonic() - self._last_request)))
            self._last_request = time.monotonic()
            response = await self._client.get(url, params=params)
            response.raise_for_status()
            return response

    def accepts(self, identifier: str) -> bool:
        return normalize_arxiv_id(identifier) is not None

    async def resolve(self, identifier: str) -> PaperResolution:
        arxiv_id = normalize_arxiv_id(identifier)
        if arxiv_id is None:
            return PaperResolution(identifier=identifier, status="unresolved")
        # The official abstract page owns exact identity/version metadata. Keep
        # Atom for discovery; do not make exact resolution depend on that API.
        try:
            response = await self._get("https://arxiv.org/abs/" + arxiv_id)
            paper = _parse_abstract_page(response.text, arxiv_id, str(response.url))
            paper = paper.model_copy(
                update={"code_urls": repository_urls(paper.abstract)}
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            return PaperResolution(
                identifier=identifier,
                status="unresolved" if status == 404 else "unavailable",
                reason=f"arXiv 官方摘要页返回 HTTP {status}，未确认该论文或版本；不要猜测其他版本。",
            )
        except (httpx.RequestError, ValueError) as exc:
            return PaperResolution(
                identifier=identifier,
                status="unavailable",
                reason=f"arXiv 官方摘要页无法确认身份：{str(exc)[:300]}",
            )
        return PaperResolution(
            identifier=identifier,
            status="resolved",
            paper=paper,
        )

    async def search(
        self,
        query: str,
        *,
        date_from: date | None,
        date_to: date | None,
        open_access_only: bool,
        limit: int,
        mode: str = "keyword",
        offset: int = 0,
        sort: str = "relevance",
    ) -> tuple[Paper, ...]:
        terms = re.findall(r'"[^"]+"|[^\s]+', query)
        expression = " AND ".join(f"all:{term}" for term in terms)
        if mode == "title":
            expression = 'ti:"' + query.replace('"', " ") + '"'
        if date_from or date_to:
            start = (date_from or date(1900, 1, 1)).strftime("%Y%m%d")
            end = (date_to or date(2200, 12, 31)).strftime("%Y%m%d")
            expression = f"({expression}) AND submittedDate:[{start}0000 TO {end}2359]"
        response = await self._get(
            self._base_url,
            {
                "search_query": expression,
                "start": offset,
                "max_results": min(limit, 100),
                "sortBy": "submittedDate" if sort == "newest" else "relevance",
                "sortOrder": "descending",
            },
        )
        response.raise_for_status()
        papers = tuple(
            paper
            for paper in _parse_feed(response.text)
            if (
                date_from is None
                or (paper.publication_date and paper.publication_date >= date_from)
            )
            and (
                date_to is None
                or (paper.publication_date and paper.publication_date <= date_to)
            )
            and (not open_access_only or paper.is_open_access)
        )[:limit]
        return tuple(
            paper.model_copy(update={"code_urls": repository_urls(paper.abstract)})
            for paper in papers
        )


def _parse_abstract_page(text: str, requested_id: str, source_url: str) -> Paper:
    page = BeautifulSoup(text, "html.parser")

    def meta(name: str) -> str:
        node = page.find("meta", attrs={"name": name})
        return node.get("content", "").strip() if node else ""

    version_link = page.select_one(".arxividv a")
    version_id = (
        normalize_arxiv_id(version_link.get("href", "")) if version_link else None
    )
    work_id = re.sub(r"v\d+$", "", requested_id)
    declared_id = normalize_arxiv_id(meta("citation_arxiv_id"))
    if (
        not version_id
        or not re.fullmatch(re.escape(work_id) + r"v\d+", version_id)
        or not declared_id
        or re.sub(r"v\d+$", "", declared_id) != work_id
        or (requested_id != work_id and version_id != requested_id)
        or not meta("citation_title")
    ):
        raise ValueError("页面引用元数据与请求的论文标识或明确版本不一致")
    history = page.select_one(".submission-history")
    versions = {}
    for version, timestamp in re.findall(
        r"\[v(\d+)\]\s*(.*?UTC)", history.get_text(" ", strip=True) if history else ""
    ):
        try:
            versions[version] = parsedate_to_datetime(timestamp).date()
        except (TypeError, ValueError):
            continue
    published = versions.get("1")
    if published is None and meta("citation_date"):
        published = date.fromisoformat(meta("citation_date").replace("/", "-"))
    updated = versions.get(version_id.rsplit("v", 1)[1])
    landing = "https://arxiv.org/abs/" + version_id
    return Paper(
        canonical_id="arxiv:" + version_id,
        arxiv_id=version_id,
        aliases=("arxiv:" + version_id, version_id, "arxiv:" + work_id, work_id),
        title=meta("citation_title"),
        authors=tuple(
            node.get_text(" ", strip=True) for node in page.select(".authors a")
        ),
        abstract=meta("citation_abstract") or None,
        doi=normalize_doi(meta("citation_doi")),
        year=published.year if published else None,
        publication_date=published,
        updated_date=updated,
        venue=meta("citation_journal_title") or None,
        landing_page_url=landing,
        pdf_url="https://arxiv.org/pdf/" + version_id,
        source_urls=tuple(dict.fromkeys((landing, source_url))),
        is_open_access=True,
        open_access_status="pass",
        publication_status="preprint",
        provider="arxiv",
        source_records=(
            SourceRecord(
                provider="arxiv",
                provider_paper_id=version_id,
                canonical_id="arxiv:" + version_id,
                source_url=source_url,
                publication_date=published,
                updated_date=updated,
                identity_status="verified",
            ),
        ),
    )


def _parse_feed(xml_text: str) -> tuple[Paper, ...]:
    root = ElementTree.fromstring(xml_text)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    papers: list[Paper] = []
    for entry in root.findall("atom:entry", ns):
        identifier = (entry.findtext("atom:id", default="", namespaces=ns)).strip()
        title = " ".join(
            (entry.findtext("atom:title", default="", namespaces=ns)).split()
        )
        if not identifier or not title:
            continue
        arxiv_id = normalize_arxiv_id(identifier)
        if not arxiv_id:
            continue
        work_id = re.sub(r"v\d+$", "", arxiv_id)
        abstract = (
            " ".join(
                (entry.findtext("atom:summary", default="", namespaces=ns)).split()
            )
            or None
        )
        authors = tuple(
            name.strip()
            for name in (
                author.findtext("atom:name", default="", namespaces=ns)
                for author in entry.findall("atom:author", ns)
            )
            if name.strip()
        )
        published = entry.findtext("atom:published", default="", namespaces=ns)
        publication_date = _date(published)
        updated_date = _date(entry.findtext("atom:updated", default="", namespaces=ns))
        landing = next(
            (
                link.attrib.get("href")
                for link in entry.findall("atom:link", ns)
                if link.attrib.get("rel") == "alternate"
            ),
            identifier,
        )
        source = SourceRecord(
            provider="arxiv",
            provider_paper_id=arxiv_id,
            canonical_id=f"arxiv:{arxiv_id}",
            source_url=landing,
            publication_date=publication_date,
            updated_date=updated_date,
            identity_status="verified",
        )
        papers.append(
            Paper(
                canonical_id=f"arxiv:{arxiv_id}",
                arxiv_id=arxiv_id,
                aliases=tuple(
                    dict.fromkeys(
                        (f"arxiv:{arxiv_id}", arxiv_id, f"arxiv:{work_id}", work_id)
                    )
                ),
                title=title,
                authors=authors,
                year=publication_date.year if publication_date else None,
                publication_date=publication_date,
                updated_date=updated_date,
                affiliations=tuple(
                    Affiliation(name=name, provider="arxiv", source_url=landing)
                    for name in dict.fromkeys(
                        node.text.strip()
                        for node in entry.findall("atom:author/arxiv:affiliation", ns)
                        if node.text and node.text.strip()
                    )
                ),
                venue=entry.findtext("arxiv:journal_ref", default="", namespaces=ns)
                or None,
                abstract=abstract,
                landing_page_url=landing,
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
                source_urls=(landing,),
                is_open_access=True,
                open_access_status="pass",
                publication_status="preprint",
                provider="arxiv",
                source_records=(source,),
            )
        )
    return tuple(papers)


def _date(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value).date()
    except (TypeError, ValueError):
        return None
