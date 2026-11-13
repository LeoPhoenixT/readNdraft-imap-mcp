"""Broker-owned deadlines, admission and bounded client execution."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from time import monotonic
from typing import TypeVar

from readndraft_imap_mcp.credentials import CredentialStore
from readndraft_imap_mcp.imap.client import ImapClient

from .accounts import AccountConfig, AccountRegistry
from .common import BatchItemOutcome, batch_error
from .credential_work import CredentialWorkPool
from .limits import AccountRequestQuota, PhysicalAccountKey, RequestQuotaError, SessionPermit
from .protocol import HealthResponse, ResourceUsage, decode_request
from .settings import DEFAULT_CAPACITY, CapacitySettings

T = TypeVar("T")
Item = TypeVar("Item")


@dataclass(slots=True)
class _RequestContext:
    deadline: float
    cancelled: threading.Event
    account_ids: tuple[str, ...] = ()
    accounts: dict[str, AccountConfig] | None = None
    account_errors: dict[str, Exception] = field(default_factory=dict)
    admission_error: RequestQuotaError | None = None
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


def _worker_future(submitted: Future[T]) -> asyncio.Future[T]:
    pending = asyncio.wrap_future(submitted)
    # A shielded worker can finish after its caller was cancelled. Observe its
    # exception without altering what an active caller receives from await.
    pending.add_done_callback(lambda future: None if future.cancelled() else future.exception())
    return pending


class _ClientWork:
    """Own one client session until the last worker and cleanup really finish."""

    def __init__(
        self, context: _RequestContext, account: AccountConfig, secret: str,
        factory: Callable[[AccountConfig, str], ImapClient], executor: ThreadPoolExecutor,
        permit: SessionPermit,
    ) -> None:
        self._context = context
        self._account = account
        self._secret = secret
        self._factory = factory
        self._executor = executor
        self._permit = permit
        self._stack = ExitStack()
        self._pending: Future | None = None
        self._closing: Future[None] | None = None

    def open(self) -> ImapClient:
        try:
            self._context.check()
            instance = self._factory(self._account, self._secret)
            bind_guard = getattr(instance, "bind_request_guard", None)
            if bind_guard is not None:
                bind_guard(self._context.check, self._context.remaining)
            return self._stack.enter_context(instance)
        finally:
            self._secret = ""

    def submit(self, operation: Callable[[], T]) -> Future[T]:
        if self._closing is not None:
            raise RuntimeError("client work is already closing")
        submitted = self._executor.submit(operation)
        self._pending = submitted
        return submitted

    def close(self) -> Future[None]:
        if self._closing is not None:
            return self._closing
        closing: Future[None] = Future()
        self._closing = closing

        def released(finished: Future) -> None:
            try:
                finished.result()
            except BaseException as exc:
                failure = exc
            else:
                failure = None
            finally:
                self._secret = ""
                self._permit.release()
            if failure is not None:
                closing.set_exception(failure)
            else:
                closing.set_result(None)

        def close_after_worker(_finished: Future | None) -> None:
            try:
                cleanup = self._executor.submit(self._stack.close)
            except BaseException:
                cleanup = Future()
                try:
                    self._stack.close()
                except BaseException as exc:
                    cleanup.set_exception(exc)
                else:
                    cleanup.set_result(None)
            cleanup.add_done_callback(released)

        if self._pending is None:
            close_after_worker(None)
        else:
            self._pending.add_done_callback(close_after_worker)
        return closing

    def result_after_close(self, submitted: Future[T]) -> Future[T]:
        result: Future[T] = Future()

        def completed(closing: Future[None]) -> None:
            try:
                closing.result()
                value = submitted.result()
            except BaseException as exc:
                result.set_exception(exc)
            else:
                result.set_result(value)

        self.close().add_done_callback(completed)
        return result


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
        max_imap_workers: int | None = None,
        max_waiting_imap_work: int | None = None,
        capacity_settings: CapacitySettings = DEFAULT_CAPACITY,
    ) -> None:
        self._accounts = accounts or AccountRegistry(())
        self._accounts_loader = accounts_loader
        self._credentials = credentials
        self._client_factory = client_factory
        self._settings = capacity_settings
        self._quota = quota or AccountRequestQuota(
            max_concurrent=capacity_settings.account_sessions,
            task_bucket_capacity=capacity_settings.task_bucket_capacity,
            refill_per_second=capacity_settings.task_refill_per_second,
        )
        if request_timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        self._request_timeout_seconds = request_timeout_seconds
        max_imap_workers = capacity_settings.imap_workers if max_imap_workers is None else max_imap_workers
        max_waiting_imap_work = (capacity_settings.waiting_imap_work
                                 if max_waiting_imap_work is None else max_waiting_imap_work)
        if max_imap_workers < 1 or max_waiting_imap_work < 0:
            raise ValueError("IMAP worker limits must be valid")
        self._executor = ThreadPoolExecutor(max_workers=max_imap_workers, thread_name_prefix="readndraft-imap")
        self._quota.configure_scheduler(workers=max_imap_workers, waiting=max_waiting_imap_work,
                                        account_waiting=capacity_settings.waiting_account_work)
        self._credential_pool = CredentialWorkPool(capacity_settings.credential_workers,
                                                   capacity_settings.waiting_credential_work)
        self._closed = False
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

    async def _with_request_context(
        self, operation: Callable[[], Awaitable[T]], *, account_ids: tuple[str, ...] = (),
    ) -> T:
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
        with context.admission_lock:
            if context.accounts is None:
                registry = self._current_accounts()
                resolved: dict[str, AccountConfig] = {}
                for item in context.account_ids or (account_id,):
                    try:
                        resolved[item] = registry.require_enabled(item)
                    except (KeyError, PermissionError) as exc:
                        context.account_errors[item] = exc
                context.accounts = resolved
                keys = tuple(dict.fromkeys(self._physical_account_key(item) for item in resolved.values()))
                if keys:
                    try:
                        self._quota.admit_task(keys)
                    except RequestQuotaError as exc:
                        context.admission_error = exc
            if account_id in context.account_errors:
                raise context.account_errors[account_id]
            if account_id not in context.accounts:
                raise RuntimeError("request used an account outside its admission set")
            if context.admission_error is not None:
                raise context.admission_error
            return context.accounts[account_id]

    async def _credential(self, account_id: str, permit: SessionPermit) -> str:
        context = self._context()
        submitted: Future[str] | None = None
        try:
            context.check()
            if self._credentials is None:
                raise RuntimeError("credential store is not configured")
            store = self._credentials

            def load() -> str:
                synchronous = getattr(store, "load_secret_sync", None)
                if synchronous is not None:
                    return synchronous(account_id)
                # Legacy asynchronous adapters run on an owned worker loop.
                # asyncio.run also drains any to_thread work before returning.
                return asyncio.run(store.load_secret(account_id))

            def check() -> None:
                context.check()
                if self._closed:
                    raise RuntimeError("broker execution context is closed")

            submitted = self._credential_pool.submit(check, load)
            timeout = asyncio.timeout(context.remaining())
            try:
                async with timeout:
                    return await asyncio.shield(_worker_future(submitted))
            except TimeoutError:
                if timeout.expired():
                    context.cancelled.set()
                raise
        except BaseException:
            # A timed-out/cancelled caller cannot release physical ownership.
            # Worker callbacks are independent of the caller's event loop.
            if submitted is None:
                permit.release()
            else:
                submitted.add_done_callback(lambda finished: permit.release())
            raise

    async def _acquire_client_work(self, context: _RequestContext, account_id: str) -> _ClientWork:
        context.check()
        if self._closed:
            raise RuntimeError("broker execution context is closed")
        account = self._ensure_task_admitted(context, account_id)
        permit = await self._quota.acquire_session(self._physical_account_key(account), context.deadline)
        secret = await self._credential(account_id, permit)
        return _ClientWork(context, account, secret, self._client_factory, self._executor, permit)

    async def _client_call(
        self, account_id: str, operation: Callable[[ImapClient], T], *, response_timeout: bool = True,
    ) -> T:
        if _request_context.get() is None:
            return await self._with_request_context(
                lambda: self._client_call(account_id, operation, response_timeout=response_timeout),
                account_ids=(account_id,),
            )
        context = self._context()
        work = await self._acquire_client_work(context, account_id)

        def run() -> T:
            client = work.open()
            context.check()
            return operation(client)

        try:
            submitted = work.submit(run)
        except BaseException:
            await asyncio.shield(_worker_future(work.close()))
            raise
        # Both operation and logout belong to this result, but their ownership
        # survives response timeout, cancellation and event-loop shutdown.
        pending = _worker_future(work.result_after_close(submitted))
        if response_timeout:
            return await asyncio.wait_for(asyncio.shield(pending), context.remaining())
        return await asyncio.shield(pending)

    async def _batch_client_call(
        self,
        account_id: str,
        items: tuple[Item, ...],
        operation: Callable[[ImapClient, Item], T],
        *,
        max_items: int,
        write: bool = False,
        before_item: Callable[[Item], Awaitable[None]] | None = None,
    ) -> tuple[BatchItemOutcome[T], ...]:
        if not items or len(items) > max_items:
            raise ValueError(f"batch must contain between 1 and {max_items} items")
        if _request_context.get() is None:
            return await self._with_request_context(
                lambda: self._batch_client_call(
                    account_id, items, operation, max_items=max_items, write=write, before_item=before_item,
                ),
                account_ids=(account_id,),
            )
        context = self._context()
        work = await self._acquire_client_work(context, account_id)
        try:
            client = await asyncio.shield(_worker_future(work.submit(work.open)))
            outcomes: list[BatchItemOutcome[T]] = []
            for index, item in enumerate(items):
                started = threading.Event()
                try:
                    context.check()
                    if before_item is not None:
                        await before_item(item)

                    def run(item: Item = item, started: threading.Event = started) -> T:
                        context.check()
                        started.set()
                        return operation(client, item)

                    outcomes.append(BatchItemOutcome(value=await asyncio.shield(_worker_future(work.submit(run)))))
                except Exception as exc:
                    error = batch_error(exc, outcome_unknown=write and started.is_set())
                    outcomes.append(BatchItemOutcome(error=error))
                    if error.code == "outcome_unknown" or context.cancelled.is_set() or context.remaining() <= 0:
                        remaining_error = (
                            batch_error(OSError("mail server connection lost"))
                            if error.reason == "transport_loss"
                            else batch_error(TimeoutError("broker request deadline expired"))
                        )
                        outcomes.extend(BatchItemOutcome(error=remaining_error) for _ in items[index + 1 :])
                        break
            return tuple(outcomes)
        except asyncio.CancelledError:
            context.cancelled.set()
            raise
        finally:
            await asyncio.shield(_worker_future(work.close()))

    def close(self, *, wait: bool = True) -> None:
        """Reject new work, expire waiters and drain owned physical work."""
        self._closed = True
        self._quota.close()
        self._credential_pool.close(wait=wait)
        # Client cleanup falls back to the finishing worker after shutdown.
        self._executor.shutdown(wait=wait)

    def resource_snapshot(self) -> dict[str, object]:
        from dataclasses import asdict

        usage = {**asdict(ResourceUsage()), **self._quota.usage()}
        credential = self._credential_pool.usage()
        usage["credential_running"] = credential["credential_running"]
        usage["credential_queued"] = credential["credential_queued"]
        usage["rejections"]["credential_capacity"] = credential["credential_capacity"]
        return {
            "resource_limits": {
                **asdict(self._settings),
                **self._quota.limits(),
                "imap_workers": self._max_imap_workers,
                "waiting_imap_work": self._max_waiting_imap_work,
            },
            "resource_usage": usage,
        }
