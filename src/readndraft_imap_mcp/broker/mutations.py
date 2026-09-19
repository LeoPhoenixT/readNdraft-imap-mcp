"""Audited message flag and move broker operations."""

from __future__ import annotations

import asyncio
from time import perf_counter
from typing import TYPE_CHECKING, Protocol, TypeVar

from readndraft_imap_mcp.audit import AuditEvent, AuditSink, AuditUnavailableError
from readndraft_imap_mcp.imap.client import ImapClient
from readndraft_imap_mcp.imap.models import BatchFlagChange, BatchMoveResult, FlagChange, MessageIdentity, MoveResult

from .common import BatchItemOutcome, batch_error, validate_identity_batch

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")
Item = TypeVar("Item")


class _Execution(Protocol):
    async def _client_call(
        self, account_id: str, operation: Callable[[ImapClient], T], *, response_timeout: bool = True,
    ) -> T: ...

    async def _batch_client_call(
        self, account_id: str, items: tuple[Item, ...], operation: Callable[[ImapClient, Item], T], *,
        max_items: int, response_timeout: bool = True, write: bool = False,
    ) -> tuple[BatchItemOutcome[T], ...]: ...


def mutation_spec(operation: str) -> tuple[str, str]:
    operations = {"set_star": ("set_star", r"\Flagged"), "set_read_state": ("set_read_state", r"\Seen")}
    try:
        return operations[operation]
    except KeyError as exc:
        raise ValueError(f"unsupported mutation operation: {operation}") from exc


class MutationService:
    def __init__(self, execution: _Execution, audit: AuditSink | None) -> None:
        self._execution = execution
        self._audit = audit

    async def _mutate(
        self,
        identity: MessageIdentity,
        operation: str,
        enabled: bool,
        client_id: str | None,
    ) -> FlagChange:
        if self._audit is None:
            raise AuditUnavailableError("audit sink is required for mutations")
        method_name, state_flag = mutation_spec(operation)
        started = perf_counter()
        try:

            def mutate(client: ImapClient) -> FlagChange:
                return getattr(client, method_name)(identity, enabled)

            result = await self._execution._client_call(identity.account_id, mutate, response_timeout=False)
        except Exception as exc:
            await self._audit.record(
                AuditEvent.mutation(
                    operation=operation,
                    account_id=identity.account_id,
                    mailbox=identity.mailbox,
                    uid=identity.uid,
                    success=False,
                    duration_ms=int((perf_counter() - started) * 1000),
                    error_category=type(exc).__name__,
                    client_id=client_id,
                )
            )
            raise
        await self._audit.record(
            AuditEvent.mutation(
                operation=operation,
                account_id=identity.account_id,
                mailbox=identity.mailbox,
                uid=identity.uid,
                success=True,
                duration_ms=int((perf_counter() - started) * 1000),
                old_state=state_flag in result.old_flags,
                new_state=enabled,
                client_id=client_id,
            )
        )
        return result

    async def set_star(
        self,
        identity: MessageIdentity,
        starred: bool,
        client_id: str | None = None,
    ) -> FlagChange:
        return await self._mutate(identity, "set_star", starred, client_id)

    async def set_read_state(
        self,
        identity: MessageIdentity,
        read: bool,
        client_id: str | None = None,
    ) -> FlagChange:
        return await self._mutate(identity, "set_read_state", read, client_id)

    async def _batch_mutate(
        self,
        identities: tuple[MessageIdentity, ...],
        operation: str,
        enabled: bool,
        client_id: str | None,
    ) -> tuple[BatchFlagChange, ...]:
        if self._audit is None:
            raise AuditUnavailableError("audit sink is required for mutations")
        method_name, state_flag = mutation_spec(operation)
        account_ids, indexed_groups = validate_identity_batch(identities, max_items=50, max_accounts=3)

        async def run_account(account_id: str, indexed):
            account_items = tuple(identity for _, identity in indexed)

            def mutate(client: ImapClient, identity: MessageIdentity) -> FlagChange:
                return getattr(client, method_name)(identity, enabled)

            started = perf_counter()
            try:
                outcomes = await self._execution._batch_client_call(
                    account_id,
                    account_items,
                    mutate,
                    max_items=50,
                    response_timeout=False,
                    write=True,
                )
            except Exception as exc:
                outcomes = tuple(BatchItemOutcome[FlagChange](error=batch_error(exc)) for _ in account_items)
            duration_ms = int((perf_counter() - started) * 1000)
            return indexed, outcomes, duration_ms

        completed = await asyncio.gather(
            *(run_account(account_id, indexed_groups[account_id]) for account_id in account_ids)
        )
        results: list[BatchFlagChange | None] = [None] * len(identities)
        for indexed, outcomes, duration_ms in completed:
            for (index, identity), outcome in zip(indexed, outcomes, strict=True):
                change = outcome.value
                await self._audit.record(
                    AuditEvent.mutation(
                        operation=operation,
                        account_id=identity.account_id,
                        mailbox=identity.mailbox,
                        uid=identity.uid,
                        success=change is not None,
                        duration_ms=duration_ms,
                        old_state=(state_flag in change.old_flags if change is not None else None),
                        new_state=enabled if change is not None else None,
                        error_category=outcome.error.code if outcome.error is not None else None,
                        client_id=client_id,
                        approval_required=False,
                    )
                )
                results[index] = BatchFlagChange(
                    identity=identity,
                    ok=change is not None,
                    change=change,
                    error=outcome.error,
                )
        return tuple(item for item in results if item is not None)

    async def set_read_state_batch(
        self,
        identities: tuple[MessageIdentity, ...],
        read: bool,
        client_id: str | None = None,
    ) -> tuple[BatchFlagChange, ...]:
        return await self._batch_mutate(identities, "set_read_state", read, client_id)

    async def set_star_batch(
        self,
        identities: tuple[MessageIdentity, ...],
        starred: bool,
        client_id: str | None = None,
    ) -> tuple[BatchFlagChange, ...]:
        return await self._batch_mutate(identities, "set_star", starred, client_id)

    async def move_email(
        self,
        identity: MessageIdentity,
        destination_mailbox: str,
        client_id: str | None = None,
    ) -> MoveResult:
        if self._audit is None:
            raise AuditUnavailableError("audit sink is required for mutations")
        started = perf_counter()
        try:
            result = await self._execution._client_call(
                identity.account_id,
                lambda client: client.move_email(identity, destination_mailbox),
                response_timeout=False,
            )
        except Exception as exc:
            await self._audit.record(
                AuditEvent.movement(
                    account_id=identity.account_id,
                    mailbox=identity.mailbox,
                    uid=identity.uid,
                    destination_mailbox=destination_mailbox,
                    success=False,
                    duration_ms=int((perf_counter() - started) * 1000),
                    error_category=batch_error(exc).code,
                    client_id=client_id,
                )
            )
            raise
        destination = result.destination_identity
        await self._audit.record(
            AuditEvent.movement(
                account_id=identity.account_id,
                mailbox=identity.mailbox,
                uid=identity.uid,
                destination_mailbox=result.destination_mailbox,
                destination_uid_validity=(destination.uid_validity if destination is not None else None),
                destination_uid=destination.uid if destination is not None else None,
                movement_method=result.method,
                success=True,
                duration_ms=int((perf_counter() - started) * 1000),
                client_id=client_id,
            )
        )
        return result

    async def move_emails_batch(
        self,
        identities: tuple[MessageIdentity, ...],
        destination_mailbox: str,
        client_id: str | None = None,
    ) -> tuple[BatchMoveResult, ...]:
        if self._audit is None:
            raise AuditUnavailableError("audit sink is required for mutations")
        account_ids, _ = validate_identity_batch(identities, max_items=50, max_accounts=1, exact_accounts=True)
        account_id = account_ids[0]
        started = perf_counter()
        try:
            outcomes = await self._execution._batch_client_call(
                account_id,
                identities,
                lambda client, identity: client.move_email(identity, destination_mailbox),
                max_items=50,
                response_timeout=False,
                write=True,
            )
        except Exception as exc:
            outcomes = tuple(BatchItemOutcome[MoveResult](error=batch_error(exc)) for _ in identities)
        duration_ms = int((perf_counter() - started) * 1000)
        results: list[BatchMoveResult] = []
        for identity, outcome in zip(identities, outcomes, strict=True):
            move = outcome.value
            destination = move.destination_identity if move is not None else None
            await self._audit.record(
                AuditEvent.movement(
                    account_id=identity.account_id,
                    mailbox=identity.mailbox,
                    uid=identity.uid,
                    destination_mailbox=destination_mailbox,
                    destination_uid_validity=(destination.uid_validity if destination is not None else None),
                    destination_uid=(destination.uid if destination is not None else None),
                    movement_method=move.method if move is not None else None,
                    success=move is not None,
                    duration_ms=duration_ms,
                    error_category=outcome.error.code if outcome.error is not None else None,
                    client_id=client_id,
                )
            )
            results.append(
                BatchMoveResult(
                    identity=identity,
                    ok=move is not None,
                    move=move,
                    error=outcome.error,
                )
            )
        return tuple(results)
