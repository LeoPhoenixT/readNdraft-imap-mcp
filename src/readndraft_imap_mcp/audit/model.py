from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class AuditEvent:
    timestamp: str
    operation: Literal["set_star", "set_read_state", "create_draft", "update_draft"]
    account_id: str
    mailbox: str
    uid: str
    request_size: int
    approval_required: bool
    approval_result: Literal["not_required", "approved"]
    success: bool
    duration_ms: int
    old_state: bool | None = None
    new_state: bool | None = None
    error_category: str | None = None
    client_id: str | None = None

    @classmethod
    def mutation(
        cls,
        *,
        operation: Literal["set_star", "set_read_state"],
        account_id: str,
        mailbox: str,
        uid: str,
        success: bool,
        duration_ms: int,
        old_state: bool | None = None,
        new_state: bool | None = None,
        error_category: str | None = None,
        client_id: str | None = None,
        approval_required: bool = False,
    ) -> "AuditEvent":
        return cls(
            timestamp=datetime.now(UTC).isoformat(),
            operation=operation,
            account_id=account_id,
            mailbox=mailbox,
            uid=uid,
            request_size=0,
            approval_required=approval_required,
            approval_result="approved" if approval_required else "not_required",
            success=success,
            duration_ms=max(0, duration_ms),
            old_state=old_state,
            new_state=new_state,
            error_category=error_category,
            client_id=client_id,
        )

    @classmethod
    def draft_creation(
        cls,
        *,
        account_id: str,
        mailbox: str,
        uid: str,
        request_size: int,
        success: bool,
        duration_ms: int,
        error_category: str | None = None,
        client_id: str | None = None,
    ) -> "AuditEvent":
        return cls(
            timestamp=datetime.now(UTC).isoformat(),
            operation="create_draft",
            account_id=account_id,
            mailbox=mailbox,
            uid=uid,
            request_size=max(0, request_size),
            approval_required=False,
            approval_result="not_required",
            success=success,
            duration_ms=max(0, duration_ms),
            error_category=error_category,
            client_id=client_id,
        )

    @classmethod
    def draft_update(
        cls,
        *,
        account_id: str,
        mailbox: str,
        uid: str,
        request_size: int,
        success: bool,
        duration_ms: int,
        error_category: str | None = None,
        client_id: str | None = None,
    ) -> "AuditEvent":
        return cls(
            timestamp=datetime.now(UTC).isoformat(),
            operation="update_draft",
            account_id=account_id,
            mailbox=mailbox,
            uid=uid,
            request_size=max(0, request_size),
            approval_required=False,
            approval_result="not_required",
            success=success,
            duration_ms=max(0, duration_ms),
            error_category=error_category,
            client_id=client_id,
        )

    def to_dict(self) -> dict:
        return asdict(self)


class AuditSink(Protocol):
    async def record(self, event: AuditEvent) -> None: ...


class AuditUnavailableError(RuntimeError):
    """Raised before mutation when no mandatory audit sink is configured."""
