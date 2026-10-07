"""Selective DOI metadata; a request identifier is never evidence of identity."""

import json
from datetime import date
from urllib.parse import quote

from paper_search.domain import (
    Affiliation,
    Paper,
    PaperResolution,
    SourceContent,
    SourceRecord,
)
from paper_search.retrieval.identity import normalize_doi


class CrossrefResolver:
    def __init__(self, client):
        self.client = client

    def accepts(self, identifier):
        return bool(normalize_doi(identifier))

    async def resolve(self, identifier):
        doi = normalize_doi(identifier)
        url = "https://api.crossref.org/works/" + quote(doi, safe="")
        response = await self.client.get(url)
        response.raise_for_status()
        data = response.json()["message"]
        actual = normalize_doi(data.get("DOI", ""))
        title = " ".join(data.get("title", []))
        if actual != doi or not title:
            return PaperResolution(
                identifier=identifier,
                status="unavailable",
                reason="Crossref 返回 DOI 或论文标题不匹配",
            )
        parts = data.get("published", {}).get("date-parts", [[]])[0]
        published = date(*parts) if len(parts) == 3 else None
        affiliations = tuple(
            Affiliation(name=aff["name"], provider="crossref", source_url=url)
            for author in data.get("author", [])
            for aff in author.get("affiliation", [])
            if aff.get("name")
        )
        paper = Paper(
            canonical_id="doi:" + actual,
            doi=actual,
            title=title,
            authors=tuple(
                " ".join(filter(None, (item.get("given"), item.get("family"))))
                for item in data.get("author", [])
            ),
            year=parts[0] if parts else None,
            publication_date=published,
            affiliations=affiliations,
            venue=" ".join(data.get("container-title", [])) or None,
            publication_status="published"
            if data.get("type") in {"journal-article", "proceedings-article"}
            else "unknown",
            landing_page_url=data.get("URL") or "https://doi.org/" + actual,
            source_records=(
                SourceRecord(
                    provider="crossref",
                    provider_paper_id=actual,
                    canonical_id="doi:" + actual,
                    source_url=url,
                    publication_date=published,
                    identity_status="verified",
                ),
            ),
        )
        return PaperResolution(
            identifier=identifier,
            status="resolved",
            paper=paper,
            source=SourceContent(url=url, text=json.dumps(data, ensure_ascii=False)),
        )
