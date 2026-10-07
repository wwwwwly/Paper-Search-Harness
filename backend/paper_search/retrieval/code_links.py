from __future__ import annotations

import re

_REPOSITORY_URL = re.compile(
    r"https?://(?:www\.)?(?:github\.com|gitlab\.com|bitbucket\.org)/[^\s<>()\[\]{}\"']+",
    re.IGNORECASE,
)


def repository_urls(abstract: str | None) -> tuple[str, ...]:
    """Extract only repository hosts explicitly linked by the paper abstract."""
    return tuple(
        dict.fromkeys(
            match.rstrip(".,;:!?") for match in _REPOSITORY_URL.findall(abstract or "")
        )
    )
