"""Bounded message and attachment read broker operations."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Protocol, TypeVar

from readndraft_imap_mcp.attachments import AttachmentExchange, InputAttachment, SavedAttachment
from readndraft_imap_mcp.imap.client import ImapClient
from readndraft_imap_mcp.imap.models import BatchMessageContent, HtmlContent, MessageContent, MessageIdentity
from readndraft_imap_mcp.mime.parser import MAX_MESSAGE_BYTES, MAX_TEXT_BYTES

from .common import BatchItemOutcome, batch_error, validate_identity_batch

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


T = TypeVar("T")
Item = TypeVar("Item")


class _Deadline(Protocol):
    def check(self) -> None: ...
    def remaining(self) -> float: ...


class _Execution(Protocol):
    def _context(self) -> _Deadline: ...

    async def _client_call(
        self,
        account_id: str,
        operation: Callable[[ImapClient], T],
        *,
        response_timeout: bool = True,
    ) -> T: ...

    async def _batch_client_call(
        self,
        account_id: str,
        items: tuple[Item, ...],
        operation: Callable[[ImapClient, Item], T],
        *,
        max_items: int,
        response_timeout: bool = True,
        write: bool = False,
        before_item: Callable[[Item], Awaitable[None]] | None = None,
    ) -> tuple[BatchItemOutcome[T], ...]: ...


def _validate_text_preview(max_text_chars: int | None) -> None:
    if max_text_chars is not None and (isinstance(max_text_chars, bool) or not 1 <= max_text_chars <= 100_000):
        raise ValueError("max_text_chars must be between 1 and 100000")


def _truncate_message_text(message: MessageContent, max_text_chars: int | None) -> MessageContent:
    total = len(message.text)
    if max_text_chars is None or total <= max_text_chars:
        return replace(message, text_total_chars=total, text_truncated=False)
    return replace(message, text=message.text[:max_text_chars], text_total_chars=total, text_truncated=True)


class _BatchBudget:
    """Settle concurrent downloads in request order without locking I/O."""

    def __init__(self, source_bytes: int, text_bytes: int, deadline: _Deadline) -> None:
        self._remaining_source = source_bytes
        self._remaining_text = text_bytes
        self._reserve_next = 0
        self._settle_next = 0
        self._reservations: dict[int, int] = {}
        self._skipped: set[int] = set()
        self._deadline = deadline
        self._condition = threading.Condition()

    def _advance_skipped(self) -> None:
        while self._reserve_next in self._skipped:
            self._reserve_next += 1
        while self._settle_next in self._skipped:
            self._settle_next += 1
        self._condition.notify_all()

    async def wait_turn(self, item: tuple[int, MessageIdentity]) -> None:
        """Wait outside the bounded worker pool for the next reservation."""
        index, _ = item
        while True:
            self._deadline.check()
            with self._condition:
                if index == self._reserve_next:
                    return
            await asyncio.sleep(min(0.01, max(0, self._deadline.remaining())))

    def _wait(self) -> None:
        self._deadline.check()
        self._condition.wait(timeout=min(0.05, max(0, self._deadline.remaining())))

    def skip(self, index: int) -> None:
        with self._condition:
            if index < self._settle_next or index in self._skipped:
                return
            self._skipped.add(index)
            reserved = self._reservations.pop(index, None)
            if reserved is not None:
                self._remaining_source += reserved
            self._advance_skipped()

    def reserve(self, index: int, source_bytes: int) -> bool:
        with self._condition:
            while index != self._reserve_next:
                self._wait()
            self._deadline.check()
            self._reserve_next += 1
            if source_bytes > self._remaining_source:
                self._condition.notify_all()
                return False
            self._remaining_source -= source_bytes
            self._reservations[index] = source_bytes
            self._condition.notify_all()
            return True

    def settle(self, index: int, source_bytes: int, text_bytes: int) -> None:
        """Atomically account a completed download in stable request order."""
        with self._condition:
            while index != self._settle_next:
                self._wait()
            try:
                reserved = self._reservations.pop(index, 0)
                if source_bytes > reserved + self._remaining_source:
                    raise ValueError("message exceeds the remaining retrieval limit")
                if text_bytes > self._remaining_text:
                    raise ValueError("batch plain-text response exceeds 2 MB")
                self._remaining_source += reserved - source_bytes
                self._remaining_text -= text_bytes
            finally:
                self._settle_next += 1
                self._advance_skipped()


class ReadService:
    def __init__(self, execution: _Execution, attachments: AttachmentExchange | None) -> None:
        self._execution = execution
        self._attachments = attachments

    async def get_email(self, identity: MessageIdentity, max_text_chars: int | None = None) -> MessageContent:
        _validate_text_preview(max_text_chars)
        message = await self._execution._client_call(identity.account_id, lambda client: client.get_message(identity))
        return _truncate_message_text(message, max_text_chars)

    async def get_emails(
        self, identities: tuple[MessageIdentity, ...], max_text_chars: int | None = None
    ) -> tuple[BatchMessageContent, ...]:
        account_ids, indexed_groups = validate_identity_batch(identities, max_items=10, max_accounts=2)
        _validate_text_preview(max_text_chars)

        budget = _BatchBudget(MAX_MESSAGE_BYTES, MAX_TEXT_BYTES, self._execution._context())

        async def run_account(account_id: str, indexed):
            def read(client: ImapClient, item: tuple[int, MessageIdentity]) -> MessageContent:
                index, identity = item
                try:
                    # No shared lock covers this transport operation.  The
                    # completed bytes are settled afterwards in request order.
                    budgeted = getattr(client, "get_message_budgeted", None)
                    if callable(budgeted):
                        message = budgeted(
                            identity,
                            lambda source_bytes: budget.reserve(index, source_bytes),
                        )
                    else:
                        budget.reserve(index, 0)
                        message = client.get_message(identity, MAX_MESSAGE_BYTES)
                except Exception:
                    budget.skip(index)
                    raise
                message = _truncate_message_text(message, max_text_chars)
                budget.settle(index, message.source_size, len(message.text.encode("utf-8")))
                return message

            account_items = tuple(indexed)
            try:
                outcomes = await self._execution._batch_client_call(
                    account_id, account_items, read, max_items=10, before_item=budget.wait_turn
                )
            except Exception as exc:
                outcomes = tuple(BatchItemOutcome[MessageContent](error=batch_error(exc)) for _ in account_items)
            finally:
                # Includes failures before client entry, unstarted deadline
                # items and cancellation. skip() never waits on the event loop.
                for index, _ in account_items:
                    budget.skip(index)
            return indexed, outcomes

        completed = await asyncio.gather(
            *(run_account(account_id, indexed_groups[account_id]) for account_id in account_ids)
        )
        results: list[BatchMessageContent | None] = [None] * len(identities)
        for indexed, outcomes in completed:
            for (index, identity), outcome in zip(indexed, outcomes, strict=True):
                results[index] = BatchMessageContent(
                    identity=identity, ok=outcome.value is not None,
                    message=outcome.value, error=outcome.error,
                )
        return tuple(item for item in results if item is not None)

    async def get_email_html(self, identity: MessageIdentity) -> HtmlContent:
        return await self._execution._client_call(identity.account_id, lambda client: client.get_html(identity))

    def list_attachment_inputs(self) -> tuple[InputAttachment, ...]:
        if self._attachments is None:
            raise RuntimeError("attachment exchange is not configured")
        return self._attachments.list_inputs()

    async def save_attachment(self, identity: MessageIdentity, attachment_id: str) -> SavedAttachment:
        if self._attachments is None:
            raise RuntimeError("attachment exchange is not configured")
        attachment = await self._execution._client_call(
            identity.account_id, lambda client: client.get_attachment(identity, attachment_id)
        )
        return self._attachments.save(
            attachment.metadata.filename, attachment.metadata.content_type, attachment.content
        )
