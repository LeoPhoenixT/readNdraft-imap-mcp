"""Shared bounded-broker outcomes, request validation, and error categories."""

from __future__ import annotations

import imaplib
from dataclasses import dataclass
from typing import Generic, TypeVar

from readndraft_imap_mcp.drafts import DraftBusyError, DraftRecoveryRequiredError
from readndraft_imap_mcp.imap.client import ImapClientError, ImapMovePartialError
from readndraft_imap_mcp.imap.models import MessageIdentity
from readndraft_imap_mcp.safe_error import SafeError, SafeErrorScope

from .limits import RequestQuotaError

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class BatchItemOutcome(Generic[T]):
    """One bounded batch result with a deliberately non-sensitive error category."""

    value: T | None = None
    error: SafeError | None = None


def batch_error(
    exc: Exception,
    *,
    scope: SafeErrorScope = "item",
    outcome_unknown: bool = False,
) -> SafeError:
    if outcome_unknown and isinstance(exc, (TimeoutError, OSError, imaplib.IMAP4.abort)):
        return SafeError(
            "outcome_unknown",
            "write outcome is unknown; do not retry automatically",
            scope,
            "request_deadline" if isinstance(exc, TimeoutError) else "transport_loss",
        )
    if isinstance(exc, ImapMovePartialError):
        return SafeError("partial_move", "move may have copied the message; inspect both mailboxes", scope)
    if isinstance(exc, PermissionError):
        return SafeError("permission_denied", "request denied", scope)
    if isinstance(exc, KeyError):
        return SafeError("not_found", "requested resource was not found", scope)
    if isinstance(exc, ValueError):
        return SafeError("invalid_request", "request rejected", scope)
    if isinstance(exc, RequestQuotaError):
        return SafeError(
            exc.code,  # type: ignore[arg-type]
            "account task rate limit exceeded" if exc.reason == "task_rate" else "request capacity exceeded",
            "account" if exc.reason in {"task_rate", "session_queue_timeout"} else "broker",
            exc.reason,  # type: ignore[arg-type]
            exc.retry_after_seconds,
        )
    if isinstance(exc, TimeoutError):
        return SafeError("timeout", "broker request timed out", scope, "request_deadline")
    if isinstance(exc, DraftBusyError):
        return SafeError("draft_busy", "draft update is already in progress", scope)
    if isinstance(exc, DraftRecoveryRequiredError):
        return SafeError("recovery_required", "draft update recovery is required", scope)
    if isinstance(exc, ImapClientError):
        return SafeError("imap_error", "IMAP operation failed", scope)
    if isinstance(exc, (OSError, imaplib.IMAP4.abort)):
        return SafeError("connection_error", "mail server connection failed", scope, "transport_loss")
    return SafeError("broker_error", "broker request failed", scope)


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
