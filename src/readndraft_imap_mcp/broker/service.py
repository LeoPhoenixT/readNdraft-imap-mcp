from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from time import monotonic
from typing import TypeVar

from readndraft_imap_mcp.attachments import AttachmentExchange, InputAttachment, SavedAttachment
from readndraft_imap_mcp.audit import AuditSink
from readndraft_imap_mcp.credentials import CredentialStore
from readndraft_imap_mcp.drafts import FileDraftStore
from readndraft_imap_mcp.imap.client import ImapClient
from readndraft_imap_mcp.imap.models import (
    BatchFlagChange,
    BatchMessageContent,
    BatchMoveResult,
    DraftCreationResult,
    DraftUpdateResult,
    FlagChange,
    HtmlContent,
    Mailbox,
    MailboxBatchResult,
    MessageContent,
    MessageIdentity,
    MoveResult,
    SearchFilters,
    SearchPage,
    SearchResult,
)

from .accounts import AccountConfig, AccountRegistry
from .common import BatchItemOutcome, batch_error
from .drafts import reply_thread
from .limits import AccountRequestQuota, PhysicalAccountKey, RequestQuotaError
from .mutations import mutation_spec
from .protocol import HealthResponse, decode_request

T = TypeVar("T")
Item = TypeVar("Item")
@dataclass(slots=True)
class _RequestContext:
    deadline: float
    cancelled: threading.Event
    account_ids: tuple[str, ...] = ()
    admitted_keys: frozenset[PhysicalAccountKey] = frozenset()
    admission_lock: threading.Lock = field(default_factory=threading.Lock)

    def remaining(self) -> float:
        return self.deadline - monotonic()

    def check(self) -> None:
        if self.cancelled.is_set() or self.remaining() <= 0:
            self.cancelled.set()
            raise TimeoutError("broker request deadline expired")


_request_context: contextvars.ContextVar[_RequestContext | None] = contextvars.ContextVar(
    "readndraft_request_context", default=None
)


class BrokerExecutionContext:
    """Shared account, quota, deadline, and bounded IMAP execution context."""

    def __init__(
        self,
        accounts: AccountRegistry | None = None,
        credentials: CredentialStore | None = None,
        client_factory: Callable[[AccountConfig, str], ImapClient] = ImapClient,
        quota: AccountRequestQuota | None = None,
        request_timeout_seconds: float = 30,
        accounts_loader: Callable[[], AccountRegistry] | None = None,
        max_imap_workers: int = 8,
        max_waiting_imap_work: int = 16,
    ) -> None:
        self._accounts = accounts or AccountRegistry(())
        self._accounts_loader = accounts_loader
        self._credentials = credentials
        self._client_factory = client_factory
        self._quota = quota or AccountRequestQuota()
        if request_timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        self._request_timeout_seconds = request_timeout_seconds
        if max_imap_workers < 1 or max_waiting_imap_work < 0:
            raise ValueError("IMAP worker limits must be valid")
        self._executor = ThreadPoolExecutor(max_workers=max_imap_workers, thread_name_prefix="readndraft-imap")
        self._work_capacity = threading.BoundedSemaphore(max_imap_workers + max_waiting_imap_work)
        self._max_imap_workers = max_imap_workers
        self._max_waiting_imap_work = max_waiting_imap_work

    def handle(self, payload: object) -> dict[str, object]:
        decode_request(payload)
        health = HealthResponse().to_dict()
        health.update(self.resource_snapshot())
        return health

    def list_accounts(self) -> list[dict[str, str | int | bool | None]]:
        return self._current_accounts().list_safe()

    def _current_accounts(self) -> AccountRegistry:
        return self._accounts_loader() if self._accounts_loader is not None else self._accounts

    def _context(self) -> _RequestContext:
        context = _request_context.get()
        if context is None:
            return _RequestContext(monotonic() + self._request_timeout_seconds, threading.Event())
        return context

    async def _with_request_context(self, operation, *, account_ids: tuple[str, ...] = ()):
        context = _request_context.get()
        if context is not None:
            return await operation()
        created = _RequestContext(
            monotonic() + self._request_timeout_seconds,
            threading.Event(),
            tuple(dict.fromkeys(account_ids)),
        )
        token = _request_context.set(created)
        try:
            return await operation()
        finally:
            created.cancelled.set()
            _request_context.reset(token)

    @staticmethod
    def _physical_account_key(account: AccountConfig) -> PhysicalAccountKey:
        return (account.hostname.rstrip(".").casefold(), account.port, account.username)

    def _ensure_task_admitted(self, context: _RequestContext, account_id: str) -> AccountConfig:
        account = self._current_accounts().require_enabled(account_id)
        with context.admission_lock:
            key = self._physical_account_key(account)
            if context.admitted_keys:
                if key not in context.admitted_keys:
                    raise RuntimeError("request used an account outside its admission set")
                return account
            account_ids = context.account_ids or (account_id,)
            accounts = tuple(self._current_accounts().require_enabled(item) for item in account_ids)
            keys = tuple(dict.fromkeys(self._physical_account_key(item) for item in accounts))
            self._quota.admit_task(keys)
            context.admitted_keys = frozenset(keys)
        return account

    async def _credential(self, account_id: str, account: AccountConfig) -> str:
        context = self._context()
        context.check()
        if self._credentials is None:
            raise RuntimeError("credential store is not configured")
        secret = await asyncio.wait_for(self._credentials.load_secret(account_id), context.remaining())
        return secret

    async def _client_call(
        self,
        account_id: str,
        operation: Callable[[ImapClient], T],
        *,
        response_timeout: bool = True,
    ) -> T:
        if _request_context.get() is None:
            return await self._with_request_context(
                lambda: self._client_call(account_id, operation, response_timeout=response_timeout),
                account_ids=(account_id,),
            )
        context = self._context()
        context.check()
        account = self._ensure_task_admitted(context, account_id)
        account_key = self._physical_account_key(account)

        if not self._work_capacity.acquire(blocking=False):
            self._quota.record_rejection("imap_worker_capacity")
            raise RequestQuotaError(
                "broker IMAP worker capacity exceeded",
                reason="imap_worker_capacity",
            )
        try:
            permit = await self._quota.acquire_session(account_key, context.deadline)
        except BaseException:
            self._work_capacity.release()
            raise
        try:
            secret = await self._credential(account_id, account)
        except BaseException:
            permit.release()
            self._work_capacity.release()
            raise

        def run() -> T:
            nonlocal secret
            try:
                context.check()
                instance = self._client_factory(account, secret)
                bind_guard = getattr(instance, "bind_request_guard", None)
                if bind_guard is not None:
                    bind_guard(context.check, context.remaining)
                with instance as client:
                    context.check()
                    return operation(client)
            finally:
                secret = ""

        try:
            submitted = self._executor.submit(run)
        except BaseException:
            permit.release()
            self._work_capacity.release()
            raise

        def done(_future) -> None:
            try:
                permit.release()
            finally:
                self._work_capacity.release()

        submitted.add_done_callback(done)
        pending = asyncio.wrap_future(submitted)
        if response_timeout:
            return await asyncio.wait_for(asyncio.shield(pending), context.remaining())
        return await pending

    async def _batch_client_call(
        self,
        account_id: str,
        items: tuple[Item, ...],
        operation: Callable[[ImapClient, Item], T],
        *,
        max_items: int,
        response_timeout: bool = True,
        write: bool = False,
    ) -> tuple[BatchItemOutcome[T], ...]:
        """Run a bounded account batch sequentially on one authenticated session."""
        if not items or len(items) > max_items:
            raise ValueError(f"batch must contain between 1 and {max_items} items")
        if _request_context.get() is None:
            return await self._with_request_context(
                lambda: self._batch_client_call(
                    account_id,
                    items,
                    operation,
                    max_items=max_items,
                    response_timeout=response_timeout,
                    write=write,
                ),
                account_ids=(account_id,),
            )
        context = self._context()
        context.check()
        account = self._ensure_task_admitted(context, account_id)
        account_key = self._physical_account_key(account)

        if not self._work_capacity.acquire(blocking=False):
            self._quota.record_rejection("imap_worker_capacity")
            raise RequestQuotaError(
                "broker IMAP worker capacity exceeded",
                reason="imap_worker_capacity",
            )
        try:
            permit = await self._quota.acquire_session(account_key, context.deadline)
        except BaseException:
            self._work_capacity.release()
            raise
        try:
            secret = await self._credential(account_id, account)
        except BaseException:
            permit.release()
            self._work_capacity.release()
            raise

        def run() -> tuple[BatchItemOutcome[T], ...]:
            nonlocal secret
            try:
                context.check()
                instance = self._client_factory(account, secret)
                bind_guard = getattr(instance, "bind_request_guard", None)
                if bind_guard is not None:
                    bind_guard(context.check, context.remaining)
                with instance as client:
                    outcomes: list[BatchItemOutcome[T]] = []
                    for index, item in enumerate(items):
                        try:
                            context.check()
                            outcomes.append(BatchItemOutcome(value=operation(client, item)))
                        except Exception as exc:
                            error = batch_error(exc, outcome_unknown=write)
                            outcomes.append(BatchItemOutcome(error=error))
                            if error.code == "outcome_unknown":
                                remaining_error = (
                                    batch_error(TimeoutError("broker request deadline expired"))
                                    if error.reason == "request_deadline"
                                    else batch_error(OSError("mail server connection lost"))
                                )
                                outcomes.extend(
                                    BatchItemOutcome(error=remaining_error)
                                    for _ in items[index + 1 :]
                                )
                                break
                            if context.cancelled.is_set() or context.remaining() <= 0:
                                timeout_error = batch_error(TimeoutError("broker request deadline expired"))
                                outcomes.extend(
                                    BatchItemOutcome(error=timeout_error) for _ in items[index + 1 :]
                                )
                                break
                    return tuple(outcomes)
            finally:
                secret = ""

        try:
            submitted = self._executor.submit(run)
        except BaseException:
            permit.release()
            self._work_capacity.release()
            raise

        def done(_future) -> None:
            try:
                permit.release()
            finally:
                self._work_capacity.release()

        submitted.add_done_callback(done)
        pending = asyncio.wrap_future(submitted)
        # A started batch owns completion of its ordered outcome list. The
        # request guard stops new items at the deadline while the worker retains
        # every completed result for the response.
        return await pending

    def resource_snapshot(self) -> dict[str, object]:
        return {
            "resource_limits": {
                **self._quota.limits(),
                "imap_workers": self._max_imap_workers,
                "waiting_imap_work": self._max_waiting_imap_work,
            },
            "resource_usage": self._quota.usage(),
        }

# Compatibility aliases for callers that used the former monolithic module.
_reply_thread = reply_thread
_mutation_spec = mutation_spec


class BrokerService:
    """Compatibility facade composed from explicit broker domain services."""

    def __init__(
        self,
        accounts: AccountRegistry | None = None,
        credentials: CredentialStore | None = None,
        client_factory: Callable[[AccountConfig, str], ImapClient] = ImapClient,
        audit: AuditSink | None = None,
        drafts: FileDraftStore | None = None,
        attachments: AttachmentExchange | None = None,
        quota: AccountRequestQuota | None = None,
        request_timeout_seconds: float = 30,
        accounts_loader: Callable[[], AccountRegistry] | None = None,
        max_imap_workers: int = 8,
        max_waiting_imap_work: int = 16,
    ) -> None:
        from .services import DraftService, MutationService, ReadService, SearchService

        self._execution = BrokerExecutionContext(
            accounts=accounts,
            credentials=credentials,
            client_factory=client_factory,
            quota=quota,
            request_timeout_seconds=request_timeout_seconds,
            accounts_loader=accounts_loader,
            max_imap_workers=max_imap_workers,
            max_waiting_imap_work=max_waiting_imap_work,
        )
        self._search = SearchService(self._execution)
        self._read = ReadService(self._execution, attachments)
        self._drafts = DraftService(self._execution, audit, drafts, attachments)
        self._mutations = MutationService(self._execution, audit)

    def handle(self, payload: object) -> dict[str, object]:
        return self._execution.handle(payload)

    def list_accounts(self) -> list[dict[str, str | int | bool | None]]:
        return self._execution.list_accounts()

    def resource_snapshot(self) -> dict[str, object]:
        return self._execution.resource_snapshot()

    # Retained for focused scheduler tests and internal execution adapters.
    async def _client_call(
        self,
        account_id: str,
        operation: Callable[[ImapClient], T],
        *,
        response_timeout: bool = True,
    ) -> T:
        return await self._execution._client_call(
            account_id, operation, response_timeout=response_timeout
        )

    async def _batch_client_call(
        self,
        account_id: str,
        items: tuple[Item, ...],
        operation: Callable[[ImapClient, Item], T],
        *,
        max_items: int,
        response_timeout: bool = True,
        write: bool = False,
    ) -> tuple[BatchItemOutcome[T], ...]:
        return await self._execution._batch_client_call(
            account_id,
            items,
            operation,
            max_items=max_items,
            response_timeout=response_timeout,
            write=write,
        )

    async def list_mailboxes(self, account_id: str) -> tuple[Mailbox, ...]:
        return await self._execution._with_request_context(
            lambda: self._search.list_mailboxes(account_id), account_ids=(account_id,)
        )

    async def list_mailboxes_batch(self, account_ids: tuple[str, ...]) -> tuple[MailboxBatchResult, ...]:
        return await self._execution._with_request_context(
            lambda: self._search.list_mailboxes_batch(account_ids), account_ids=account_ids
        )

    async def search_emails(
        self, account_id: str, mailbox: str, filters: SearchFilters, limit: int = 50
    ) -> tuple[SearchResult, ...]:
        return await self._execution._with_request_context(
            lambda: self._search.search_emails(account_id, mailbox, filters, limit),
            account_ids=(account_id,),
        )

    async def search_email_targets(
        self,
        targets: tuple[tuple[str, str], ...],
        filters: SearchFilters,
        limit: int = 50,
        cursor: str | None = None,
    ) -> SearchPage:
        account_ids = tuple(dict.fromkeys(account_id for account_id, _ in targets))
        return await self._execution._with_request_context(
            lambda: self._search.search_email_targets(targets, filters, limit, cursor),
            account_ids=account_ids,
        )

    async def get_email(
        self, identity: MessageIdentity, max_text_chars: int | None = None
    ) -> MessageContent:
        return await self._execution._with_request_context(
            lambda: self._read.get_email(identity, max_text_chars),
            account_ids=(identity.account_id,),
        )

    async def get_emails(
        self, identities: tuple[MessageIdentity, ...], max_text_chars: int | None = None
    ) -> tuple[BatchMessageContent, ...]:
        account_ids = tuple(dict.fromkeys(item.account_id for item in identities))
        return await self._execution._with_request_context(
            lambda: self._read.get_emails(identities, max_text_chars), account_ids=account_ids
        )

    async def get_email_html(self, identity: MessageIdentity) -> HtmlContent:
        return await self._execution._with_request_context(
            lambda: self._read.get_email_html(identity), account_ids=(identity.account_id,)
        )

    def list_attachment_inputs(self) -> tuple[InputAttachment, ...]:
        return self._read.list_attachment_inputs()

    async def save_attachment(self, identity: MessageIdentity, attachment_id: str) -> SavedAttachment:
        return await self._execution._with_request_context(
            lambda: self._read.save_attachment(identity, attachment_id),
            account_ids=(identity.account_id,),
        )

    async def create_draft(
        self,
        account_id: str,
        *,
        to: tuple[str, ...],
        cc: tuple[str, ...] = (),
        bcc: tuple[str, ...] = (),
        subject: str,
        body: str,
        html_body: str | None = None,
        attachment_names: tuple[str, ...] = (),
        reply_to_message: MessageIdentity | None = None,
        client_id: str | None = None,
    ) -> DraftCreationResult:
        return await self._execution._with_request_context(
            lambda: self._drafts.create_draft(
                account_id,
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                body=body,
                html_body=html_body,
                attachment_names=attachment_names,
                reply_to_message=reply_to_message,
                client_id=client_id,
            ),
            account_ids=(account_id,),
        )

    async def update_draft(
        self,
        account_id: str,
        draft_id: str,
        *,
        to: tuple[str, ...],
        cc: tuple[str, ...] = (),
        bcc: tuple[str, ...] = (),
        subject: str,
        body: str,
        html_body: str | None = None,
        attachment_names: tuple[str, ...] = (),
        client_id: str | None = None,
    ) -> DraftUpdateResult:
        return await self._execution._with_request_context(
            lambda: self._drafts.update_draft(
                account_id,
                draft_id,
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                body=body,
                html_body=html_body,
                attachment_names=attachment_names,
                client_id=client_id,
            ),
            account_ids=(account_id,),
        )

    async def set_star(
        self,
        identity: MessageIdentity,
        starred: bool,
        client_id: str | None = None,
    ) -> FlagChange:
        return await self._execution._with_request_context(
            lambda: self._mutations.set_star(identity, starred, client_id),
            account_ids=(identity.account_id,),
        )

    async def set_read_state(
        self,
        identity: MessageIdentity,
        read: bool,
        client_id: str | None = None,
    ) -> FlagChange:
        return await self._execution._with_request_context(
            lambda: self._mutations.set_read_state(identity, read, client_id),
            account_ids=(identity.account_id,),
        )

    async def set_read_state_batch(
        self,
        identities: tuple[MessageIdentity, ...],
        read: bool,
        client_id: str | None = None,
    ) -> tuple[BatchFlagChange, ...]:
        account_ids = tuple(dict.fromkeys(item.account_id for item in identities))
        return await self._execution._with_request_context(
            lambda: self._mutations.set_read_state_batch(identities, read, client_id),
            account_ids=account_ids,
        )

    async def set_star_batch(
        self,
        identities: tuple[MessageIdentity, ...],
        starred: bool,
        client_id: str | None = None,
    ) -> tuple[BatchFlagChange, ...]:
        account_ids = tuple(dict.fromkeys(item.account_id for item in identities))
        return await self._execution._with_request_context(
            lambda: self._mutations.set_star_batch(identities, starred, client_id),
            account_ids=account_ids,
        )

    async def move_email(
        self,
        identity: MessageIdentity,
        destination_mailbox: str,
        client_id: str | None = None,
    ) -> MoveResult:
        return await self._execution._with_request_context(
            lambda: self._mutations.move_email(identity, destination_mailbox, client_id),
            account_ids=(identity.account_id,),
        )

    async def move_emails_batch(
        self,
        identities: tuple[MessageIdentity, ...],
        destination_mailbox: str,
        client_id: str | None = None,
    ) -> tuple[BatchMoveResult, ...]:
        account_ids = tuple(dict.fromkeys(item.account_id for item in identities))
        return await self._execution._with_request_context(
            lambda: self._mutations.move_emails_batch(identities, destination_mailbox, client_id),
            account_ids=account_ids,
        )
