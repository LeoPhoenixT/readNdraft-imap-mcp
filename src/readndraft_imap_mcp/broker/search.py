"""Mailbox discovery and bounded message-search broker operations."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Protocol, TypeVar

from readndraft_imap_mcp.imap.client import ImapClient, ImapClientError
from readndraft_imap_mcp.imap.models import (
    Mailbox,
    MailboxBatchResult,
    SearchFilters,
    SearchPage,
    SearchResult,
    SearchTarget,
    SearchTargetError,
    SearchTargetStatus,
)
from readndraft_imap_mcp.imap.search import SearchScanBudget

from .common import batch_error

if TYPE_CHECKING:
    from collections.abc import Callable


T = TypeVar("T")


class _Execution(Protocol):
    async def _with_request_context(self, operation: Callable[[], object]) -> object: ...

    async def _client_call(
        self,
        account_id: str,
        operation: Callable[[ImapClient], T],
        *,
        response_timeout: bool = True,
        quota_cost: int = 1,
    ) -> T: ...


def _search_fingerprint(filters: SearchFilters) -> str:
    values = asdict(filters)
    values["after"] = filters.after.isoformat() if filters.after else None
    values["before"] = filters.before.isoformat() if filters.before else None
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _encode_search_cursor(
    account_id: str,
    mailbox: str,
    uid_validity: str,
    before_uid: str,
    filters: SearchFilters,
) -> str:
    payload = json.dumps(
        {
            "v": 2,
            "account_id": account_id,
            "mailbox": mailbox,
            "uid_validity": uid_validity,
            "scan_before_uid": before_uid,
            "filters": _search_fingerprint(filters),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_search_cursor(
    value: str,
    account_id: str,
    mailbox: str,
    filters: SearchFilters,
) -> tuple[str, str]:
    if not value or len(value) > 2048 or not value.isascii():
        raise ValueError("invalid search cursor")
    try:
        encoded = value.encode("ascii")
        payload = json.loads(base64.b64decode(encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid search cursor") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "v",
        "account_id",
        "mailbox",
        "uid_validity",
        "scan_before_uid",
        "filters",
    }:
        raise ValueError("invalid search cursor")
    if (
        payload["v"] != 2
        or payload["account_id"] != account_id
        or payload["mailbox"] != mailbox
        or payload["filters"] != _search_fingerprint(filters)
        or not isinstance(payload["uid_validity"], str)
        or not isinstance(payload["scan_before_uid"], str)
        or not payload["uid_validity"].isdigit()
        or not payload["scan_before_uid"].isdigit()
    ):
        raise ValueError("search cursor does not match this query")
    return payload["uid_validity"], payload["scan_before_uid"]


class SearchService:
    def __init__(self, execution: _Execution) -> None:
        self._execution = execution

    async def list_mailboxes(self, account_id: str) -> tuple[Mailbox, ...]:
        return await self._execution._with_request_context(
            lambda: self._execution._client_call(account_id, lambda client: client.list_mailboxes())
        )  # type: ignore[return-value]

    async def list_mailboxes_batch(self, account_ids: tuple[str, ...]) -> tuple[MailboxBatchResult, ...]:
        return await self._execution._with_request_context(lambda: self._list_mailboxes_batch(account_ids))  # type: ignore[return-value]

    async def _list_mailboxes_batch(self, account_ids: tuple[str, ...]) -> tuple[MailboxBatchResult, ...]:
        if not 1 <= len(account_ids) <= 10:
            raise ValueError("mailbox lookup must contain between 1 and 10 accounts")
        if len(set(account_ids)) != len(account_ids):
            raise ValueError("mailbox account ids must be unique")

        async def lookup(account_id: str) -> MailboxBatchResult:
            try:
                return MailboxBatchResult(
                    account_id=account_id,
                    ok=True,
                    mailboxes=await self.list_mailboxes(account_id),
                )
            except Exception as exc:
                return MailboxBatchResult(account_id=account_id, ok=False, error=batch_error(exc))

        return tuple(await asyncio.gather(*(lookup(account_id) for account_id in account_ids)))

    async def search_emails(
        self, account_id: str, mailbox: str, filters: SearchFilters, limit: int = 50
    ) -> tuple[SearchResult, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("search limit must be between 1 and 500")
        return await self._execution._client_call(
            account_id, lambda client: client.search(mailbox, filters, limit)
        )

    async def search_email_targets(
        self,
        targets: tuple[tuple[str, str], ...],
        filters: SearchFilters,
        limit: int = 50,
        cursor: str | None = None,
    ) -> SearchPage:
        return await self._execution._with_request_context(
            lambda: self._search_email_targets(targets, filters, limit, cursor)
        )  # type: ignore[return-value]

    async def _search_email_targets(
        self,
        targets: tuple[tuple[str, str], ...],
        filters: SearchFilters,
        limit: int = 50,
        cursor: str | None = None,
    ) -> SearchPage:
        if not targets or len(targets) > 20:
            raise ValueError("search must contain between 1 and 20 targets")
        if len(set(targets)) != len(targets):
            raise ValueError("search targets must be unique")
        if not 1 <= limit <= 500:
            raise ValueError("search limit must be between 1 and 500")
        if limit > 50 and len(targets) != 1:
            raise ValueError("searches over 50 results require exactly one target")
        if cursor is not None and len(targets) != 1:
            raise ValueError("cursor pagination requires exactly one target")
        if len(targets) == 1:
            account_id, mailbox = targets[0]
            target = SearchTarget(account_id, mailbox)
            expected_uid_validity = None
            before_uid = None
            if cursor is not None:
                expected_uid_validity, before_uid = _decode_search_cursor(cursor, account_id, mailbox, filters)
            try:
                window = await self._execution._client_call(
                    account_id,
                    lambda client: client.search_window(
                        mailbox,
                        filters,
                        limit,
                        before_uid=before_uid,
                        expected_uid_validity=expected_uid_validity,
                    ),
                )
            except Exception as exc:
                error = batch_error(exc)
                return SearchPage(
                    results=(), errors=(SearchTargetError(account_id, mailbox, error),),
                    next_cursor=None, truncated=False, order="mailbox_uid_desc",
                    targets_searched=(target,), targets_pending=(),
                    target_statuses=(SearchTargetStatus(account_id, mailbox, "error", cursor=cursor, error=error),),
                )
            next_cursor = None
            if window.has_more and window.next_uid is not None:
                next_cursor = _encode_search_cursor(
                    account_id, mailbox, window.uid_validity, window.next_uid, filters
                )
            return SearchPage(
                results=window.results, errors=(), next_cursor=next_cursor,
                truncated=window.has_more, order="mailbox_uid_desc",
                targets_searched=(target,), targets_pending=(),
                target_statuses=(
                    (
                        SearchTargetStatus(account_id, mailbox, "partial", cursor=next_cursor)
                        if next_cursor is not None
                        else SearchTargetStatus(account_id, mailbox, "complete")
                    ),
                ),
            )

        grouped: list[tuple[str, list[str]]] = []
        for account_id, mailbox in targets:
            if grouped and grouped[-1][0] == account_id:
                grouped[-1][1].append(mailbox)
            else:
                grouped.append((account_id, [mailbox]))
        results: list[SearchResult] = []
        errors: list[SearchTargetError] = []
        searched: list[SearchTarget] = []
        partial_windows: dict[SearchTarget, tuple[str, str]] = {}
        scan_budget = SearchScanBudget()
        truncated = False
        for account_id, mailboxes in grouped:
            remaining = limit - len(results)
            if remaining == 0:
                truncated = True
                break

            def search_account(
                client: ImapClient,
            ) -> tuple[tuple[SearchResult, ...], tuple[SearchTargetError, ...], tuple[SearchTarget, ...], bool]:
                matches: list[SearchResult] = []
                failures: list[SearchTargetError] = []
                attempted: list[SearchTarget] = []
                page_truncated = False
                for index, mailbox in enumerate(mailboxes):
                    mailbox_remaining = remaining - len(matches)
                    if mailbox_remaining == 0:
                        page_truncated = True
                        break
                    attempted.append(SearchTarget(account_id, mailbox))
                    try:
                        try:
                            window = client.search_window(mailbox, filters, mailbox_remaining, scan_budget=scan_budget)
                        except TypeError as exc:
                            # Test and third-party client doubles predating the
                            # internal budget argument retain the public API.
                            if "scan_budget" not in str(exc):
                                raise
                            window = client.search_window(mailbox, filters, mailbox_remaining)
                    except Exception as exc:
                        failures.append(SearchTargetError(account_id, mailbox, batch_error(exc)))
                        continue
                    matches.extend(window.results)
                    if window.has_more:
                        page_truncated = True
                        if window.next_uid is None:
                            raise ImapClientError("partial search omitted a continuation UID")
                        partial_windows[SearchTarget(account_id, mailbox)] = (window.uid_validity, window.next_uid)
                    if len(matches) == remaining and index + 1 < len(mailboxes):
                        page_truncated = True
                return tuple(matches), tuple(failures), tuple(attempted), page_truncated

            try:
                matches, failures, attempted, page_truncated = await self._execution._client_call(
                    account_id, search_account, quota_cost=len(mailboxes)
                )
            except Exception as exc:
                category = batch_error(exc)
                errors.extend(SearchTargetError(account_id, mailbox, category) for mailbox in mailboxes)
                searched.extend(SearchTarget(account_id, mailbox) for mailbox in mailboxes)
                continue
            results.extend(matches[:remaining])
            errors.extend(failures)
            searched.extend(attempted)
            truncated = truncated or page_truncated
        searched_set = set(searched)
        pending = tuple(
            SearchTarget(account_id, mailbox)
            for account_id, mailbox in targets
            if SearchTarget(account_id, mailbox) not in searched_set
        )
        error_by_target = {(item.account_id, item.mailbox): item.error for item in errors}
        pending_set = set(pending)
        statuses: list[SearchTargetStatus] = []
        for account_id, mailbox in targets:
            target = SearchTarget(account_id, mailbox)
            if target in pending_set:
                statuses.append(SearchTargetStatus(account_id, mailbox, "pending"))
            elif (error := error_by_target.get((account_id, mailbox))) is not None:
                statuses.append(SearchTargetStatus(account_id, mailbox, "error", error=error))
            elif (state := partial_windows.get(target)) is not None:
                statuses.append(SearchTargetStatus(
                    account_id, mailbox, "partial",
                    cursor=_encode_search_cursor(account_id, mailbox, state[0], state[1], filters),
                ))
            else:
                statuses.append(SearchTargetStatus(account_id, mailbox, "complete"))
        return SearchPage(
            results=tuple(results), errors=tuple(errors), next_cursor=None,
            truncated=truncated or bool(pending), order="target_then_mailbox_uid_desc",
            targets_searched=tuple(searched), targets_pending=pending,
            target_statuses=tuple(statuses),
        )
