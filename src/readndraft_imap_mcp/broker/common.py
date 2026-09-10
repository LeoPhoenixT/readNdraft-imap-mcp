"""Shared bounded-broker outcomes, request validation, and error categories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from readndraft_imap_mcp.drafts import DraftBusyError, DraftRecoveryRequiredError
from readndraft_imap_mcp.imap.client import ImapClientError, ImapMovePartialError
from readndraft_imap_mcp.imap.models import MessageIdentity

from .limits import RequestQuotaError

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class BatchItemOutcome(Generic[T]):
    """One bounded batch result with a deliberately non-sensitive error category."""

    value: T | None = None
    error: str | None = None


def batch_error(exc: Exception) -> str:
    if isinstance(exc, ImapMovePartialError):
        return "partial_move"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, KeyError):
        return "not_found"
    if isinstance(exc, ValueError):
        return "invalid_request"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, RequestQuotaError):
        return "rate_limited"
    if isinstance(exc, DraftBusyError):
        return "draft_busy"
    if isinstance(exc, DraftRecoveryRequiredError):
        return "recovery_required"
    if isinstance(exc, ImapClientError):
        return "imap_error"
    if isinstance(exc, OSError):
        return "connection_error"
    return "broker_error"


def validate_identity_batch(
    identities: tuple[MessageIdentity, ...],
    *,
    max_items: int,
    max_accounts: int,
    exact_accounts: bool = False,
) -> tuple[tuple[str, ...], dict[str, list[tuple[int, MessageIdentity]]]]:
    if not identities or len(identities) > max_items:
        raise ValueError(f"batch must contain between 1 and {max_items} identities")
    keys = [(item.account_id, item.mailbox, item.uid_validity, item.uid) for item in identities]
    if len(set(keys)) != len(keys):
        raise ValueError("batch identities must be unique")
    account_ids = tuple(dict.fromkeys(item.account_id for item in identities))
    if exact_accounts and len(account_ids) != max_accounts:
        raise ValueError("move batch must belong to exactly one account")
    if not exact_accounts and len(account_ids) > max_accounts:
        raise ValueError(f"batch may span at most {max_accounts} accounts")
    indexed_groups: dict[str, list[tuple[int, MessageIdentity]]] = {}
    for index, identity in enumerate(identities):
        indexed_groups.setdefault(identity.account_id, []).append((index, identity))
    return account_ids, indexed_groups
