"""Bounded, server-side IMAP search planning constants and helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import SearchFilters

SEARCH_FETCH_CHUNK_SIZE = 25
SEARCH_UID_RANGE_SIZE = 10_000
MAX_SEARCH_REQUESTS = 20
MAX_ATTACHMENT_CANDIDATES = 500
MAX_SEARCH_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(slots=True)
class SearchScanBudget:
    """Request-wide limits shared by sequential target scans."""
    search_requests: int = 0
    attachment_candidates: int = 0

    def take_search_request(self) -> bool:
        if self.search_requests >= MAX_SEARCH_REQUESTS:
            return False
        self.search_requests += 1
        return True

    def attachment_capacity(self) -> int:
        return MAX_ATTACHMENT_CANDIDATES - self.attachment_candidates

    def take_candidates(self, count: int) -> None:
        if not 0 <= count <= self.attachment_capacity():
            raise ValueError("attachment search candidate budget exceeded")
        self.attachment_candidates += count


def quote_search(value: str) -> str:
    if not value or len(value) > 200 or "\r" in value or "\n" in value:
        raise ValueError("search text must contain 1 to 200 characters without line breaks")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("search currently requires ASCII text") from exc
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def search_criteria(filters: SearchFilters) -> list[str | None]:
    criteria: list[str | None] = [None]
    for key, value in (
        ("FROM", filters.sender),
        ("TO", filters.recipient),
        ("SUBJECT", filters.subject),
        ("TEXT", filters.text),
    ):
        if value is not None:
            criteria.extend((key, quote_search(value)))
    if filters.after is not None:
        criteria.extend(("SINCE", _imap_date(filters.after)))
    if filters.before is not None:
        criteria.extend(("BEFORE", _imap_date(filters.before)))
    if filters.read is not None:
        criteria.append("SEEN" if filters.read else "UNSEEN")
    if filters.starred is not None:
        criteria.append("FLAGGED" if filters.starred else "UNFLAGGED")
    if len(criteria) == 1:
        criteria.append("ALL")
    return criteria


def _imap_date(value: date) -> str:
    return value.strftime("%d-%b-%Y")
