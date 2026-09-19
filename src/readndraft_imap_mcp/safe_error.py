from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SafeErrorCode = Literal[
    "partial_move",
    "permission_denied",
    "not_found",
    "invalid_request",
    "timeout",
    "rate_limited",
    "draft_busy",
    "recovery_required",
    "imap_error",
    "connection_error",
    "broker_error",
    "outcome_unknown",
]
SafeErrorScope = Literal["request", "item", "account", "broker", "client"]
SafeErrorReason = Literal[
    "task_rate",
    "session_queue_timeout",
    "imap_worker_capacity",
    "ipc_helper_capacity",
    "request_deadline",
    "transport_loss",
]
SAFE_ERROR_CODES = frozenset(
    {
        "partial_move",
        "permission_denied",
        "not_found",
        "invalid_request",
        "timeout",
        "rate_limited",
        "draft_busy",
        "recovery_required",
        "imap_error",
        "connection_error",
        "broker_error",
        "outcome_unknown",
    }
)
SAFE_ERROR_SCOPES = frozenset({"request", "item", "account", "broker", "client"})
SAFE_ERROR_REASONS = frozenset(
    {
        "task_rate",
        "session_queue_timeout",
        "imap_worker_capacity",
        "ipc_helper_capacity",
        "request_deadline",
        "transport_loss",
    }
)


@dataclass(frozen=True, slots=True)
class SafeError:
    """Stable, non-sensitive error information exposed across IPC and MCP."""

    code: SafeErrorCode
    message: str
    scope: SafeErrorScope
    reason: SafeErrorReason | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.code not in SAFE_ERROR_CODES:
            raise ValueError("invalid safe error code")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("safe error message is required")
        if self.scope not in SAFE_ERROR_SCOPES:
            raise ValueError("invalid safe error scope")
        if self.reason is not None and self.reason not in SAFE_ERROR_REASONS:
            raise ValueError("invalid safe error reason")
        if self.retry_after_seconds is not None and (
            isinstance(self.retry_after_seconds, bool)
            or not isinstance(self.retry_after_seconds, int)
            or self.retry_after_seconds < 1
        ):
            raise ValueError("retry_after_seconds must be at least 1")
