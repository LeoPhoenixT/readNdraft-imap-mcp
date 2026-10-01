"""Small structural interfaces shared by broker domain services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, TypeVar

if TYPE_CHECKING:
    from readndraft_imap_mcp.imap.client import ImapClient

    from .accounts import AccountRegistry
    from .common import BatchItemOutcome

T = TypeVar("T")
Item = TypeVar("Item")


class RequestDeadline(Protocol):
    def check(self) -> None: ...
    def remaining(self) -> float: ...


class ClientExecution(Protocol):
    async def _client_call(
        self, account_id: str, operation: Callable[[ImapClient], T], *, response_timeout: bool = True,
    ) -> T: ...


class BatchExecution(ClientExecution, Protocol):
    async def _batch_client_call(
        self,
        account_id: str,
        items: tuple[Item, ...],
        operation: Callable[[ImapClient, Item], T],
        *,
        max_items: int,
        write: bool = False,
        before_item: Callable[[Item], Awaitable[None]] | None = None,
    ) -> tuple[BatchItemOutcome[T], ...]: ...


class ReadExecution(BatchExecution, Protocol):
    def _context(self) -> RequestDeadline: ...


class SearchExecution(ClientExecution, Protocol):
    async def _with_request_context(
        self, operation: Callable[[], Awaitable[T]], *, account_ids: tuple[str, ...] = (),
    ) -> T: ...


class DraftExecution(ClientExecution, Protocol):
    def _current_accounts(self) -> AccountRegistry: ...
