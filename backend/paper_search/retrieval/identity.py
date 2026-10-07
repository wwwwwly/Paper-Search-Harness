from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from paper_search.domain import Paper, SourceRecord


def normalize_doi(value: str | None) -> str | None:
    if value is None:
        return None
    doi = value.strip().casefold()
    for prefix in (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi:",
    ):
        if doi.startswith(prefix):
            doi = doi[len(prefix) :]
            break
    doi = doi.strip().rstrip(".,;)")
    return doi if re.fullmatch(r"10\.\d{4,9}/\S+", doi) else None


def normalize_url(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip().removeprefix("url:")
    parts = urlsplit(candidate)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        return None
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), path, parts.query, "")
    )


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(c if c.isalnum() else " " for c in normalized)
    return " ".join(normalized.split())


def normalize_arxiv_id(value: str) -> str | None:
    candidate = value.strip().removesuffix(".pdf")
    match = re.fullmatch(
        r"(?:(?:https?://)?(?:export\.)?arxiv\.org/(?:abs|pdf)/|arxiv:|arxiv\.)?"
        r"(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)",
        candidate,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def identity_keys(paper: Paper) -> tuple[str, ...]:
    keys: list[str] = []
    if paper.doi:
        keys.append(f"doi:{normalize_doi(paper.doi)}")
    if paper.arxiv_id:
        keys.append(f"arxiv:{paper.arxiv_id.casefold()}")
        work_id = re.sub(r"v\d+$", "", paper.arxiv_id.casefold())
        keys.append(f"arxiv-work:{work_id}")
    for value in (
        paper.canonical_id,
        *paper.aliases,
        *paper.source_urls,
        *(record.source_url for record in paper.source_records if record.source_url),
    ):
        alias = str(value)
        key = (
            f"doi:{doi}"
            if (doi := normalize_doi(alias))
            else f"arxiv:{arxiv.casefold()}"
            if (arxiv := normalize_arxiv_id(alias))
            else f"url:{url}"
            if (url := normalize_url(alias))
            else None
        )
        if key and key not in keys:
            keys.append(key)
    return tuple(key for key in keys if key and not key.endswith(":None"))


def papers_match(first: Paper, second: Paper) -> bool:
    """Only explicit stable identifiers establish identity, never title/year alone."""
    return bool(set(identity_keys(first)) & set(identity_keys(second)))


def merge_papers(first: Paper, second: Paper) -> Paper:
    # arXiv records carry the actual first submission date. Aggregators may use
    # January 1 for year-only venue dates; taking their minimum corrupts v1 dates.
    # Keep all source records so venue publication dates remain available.
    submission_dates = [
        record.publication_date
        for paper in (first, second)
        for record in paper.source_records
        if record.provider == "arxiv" and record.publication_date
    ]
    publication_date = min(
        submission_dates
        or [item for item in (first.publication_date, second.publication_date) if item],
        default=None,
    )
    updated_date = max(
        (item for item in (first.updated_date, second.updated_date) if item),
        default=None,
    )
    doi = normalize_doi(first.doi) or normalize_doi(second.doi)
    aliases = [first.canonical_id, second.canonical_id, *first.aliases, *second.aliases]
    aliases.extend(identity_keys(first))
    aliases.extend(identity_keys(second))
    unique_aliases = tuple(dict.fromkeys(alias for alias in aliases if alias))
    arxiv_id = first.arxiv_id or second.arxiv_id
    canonical_id = (
        f"doi:{doi}"
        if doi
        else f"arxiv:{arxiv_id}"
        if arxiv_id
        else next(
            (
                f"url:{url}"
                for value in (
                    first.canonical_id,
                    second.canonical_id,
                    first.landing_page_url,
                    second.landing_page_url,
                )
                if (url := normalize_url(str(value or "")))
            ),
            first.canonical_id,
        )
    )
    source_records: list[SourceRecord] = []
    source_record_indexes: dict[tuple[str, str, str, str], int] = {}
    for record in (*first.source_records, *second.source_records):
        provider = record.provider.casefold().strip()
        provider_paper_id = record.provider_paper_id.casefold().strip()
        canonical = record.canonical_id.casefold().strip()
        source_url = str(record.source_url or "").casefold().strip()
        key = (provider, provider_paper_id, canonical, "")
        if not provider_paper_id and not canonical:
            key = (provider, "", "", source_url)
        if key in source_record_indexes:
            index = source_record_indexes[key]
            if (
                source_records[index].identity_status == "discovered"
                and record.identity_status == "verified"
            ):
                source_records[index] = record
            continue
        source_record_indexes[key] = len(source_records)
        source_records.append(record)
    return first.model_copy(
        update={
            "canonical_id": canonical_id,
            "arxiv_id": arxiv_id,
            "doi": doi,
            "aliases": unique_aliases,
            "title": first.title
            if len(first.title) >= len(second.title)
            else second.title,
            "authors": first.authors or second.authors,
            "year": publication_date.year
            if publication_date
            else first.year or second.year,
            "publication_date": publication_date,
            "updated_date": updated_date,
            "affiliations": tuple(
                dict.fromkeys((*first.affiliations, *second.affiliations))
            ),
            "venue": first.venue or second.venue,
            "abstract": (
                first.abstract
                if len(first.abstract or "") >= len(second.abstract or "")
                else second.abstract
            ),
            "landing_page_url": first.landing_page_url or second.landing_page_url,
            "pdf_url": first.pdf_url or second.pdf_url,
            "source_urls": tuple(
                dict.fromkeys((*first.source_urls, *second.source_urls))
            ),
            "is_open_access": first.is_open_access or second.is_open_access,
            "open_access_status": "pass"
            if "pass" in {first.open_access_status, second.open_access_status}
            else first.open_access_status
            if first.open_access_status != "unknown"
            else second.open_access_status,
            "publication_status": max(
                (first.publication_status, second.publication_status),
                key={
                    "unknown": 0,
                    "preprint": 1,
                    "submitted": 2,
                    "accepted": 3,
                    "published": 4,
                }.get,
            ),
            "code_urls": tuple(dict.fromkeys((*first.code_urls, *second.code_urls))),
            "source_records": tuple(source_records),
        }
    )
