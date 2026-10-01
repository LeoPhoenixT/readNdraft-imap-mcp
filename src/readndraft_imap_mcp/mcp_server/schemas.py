"""MCP input and output schemas without tool or broker runtime imports."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class IdentityOutput(BaseModel):
    account_id: str
    mailbox: str
    uid_validity: str
    uid: str


class AccountOutput(BaseModel):
    id: str
    username: str
    host: str
    port: int
    enabled: bool
    sender_address: str | None = None
    sender_name: str | None = None


class MailboxOutput(BaseModel):
    name: str
    display_name: str
    delimiter: str | None
    flags: list[str]


class SafeErrorOutput(BaseModel):
    code: Literal[
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
    message: str
    scope: Literal["request", "item", "account", "broker", "client"]
    reason: Literal[
        "task_rate",
        "session_queue_timeout",
        "imap_worker_capacity",
        "ipc_helper_capacity",
        "request_deadline",
        "transport_loss",
    ] | None
    retry_after_seconds: int | None


class MailboxBatchOutput(BaseModel):
    account_id: str
    ok: bool
    mailboxes: list[MailboxOutput]
    error: SafeErrorOutput | None


class SearchTargetInput(BaseModel):
    account_id: str
    mailbox: str


class SearchResultOutput(BaseModel):
    identity: IdentityOutput
    headers: dict[str, str]
    flags: list[str]
    size: int
    received_at: str


class SearchTargetErrorOutput(BaseModel):
    account_id: str
    mailbox: str
    error: SafeErrorOutput


class SearchTargetOutput(BaseModel):
    account_id: str
    mailbox: str


class SearchTargetStatusOutput(BaseModel):
    account_id: str
    mailbox: str
    status: Literal["complete", "partial", "error", "pending"]
    cursor: str | None
    error: SafeErrorOutput | None


class SearchPageOutput(BaseModel):
    results: list[SearchResultOutput]
    errors: list[SearchTargetErrorOutput]
    next_cursor: str | None
    truncated: bool
    order: str
    targets_searched: list[SearchTargetOutput]
    targets_pending: list[SearchTargetOutput]
    target_statuses: list[SearchTargetStatusOutput]


class AttachmentMetadataOutput(BaseModel):
    attachment_id: str
    filename: str
    content_type: str
    size: int | None
    encoded_size: int | None


class MessageOutput(BaseModel):
    identity: IdentityOutput
    headers: dict[str, str]
    text: str
    flags: list[str]
    attachments: list[AttachmentMetadataOutput]
    text_total_chars: int
    text_truncated: bool


class BatchMessageOutput(BaseModel):
    identity: IdentityOutput
    ok: bool
    message: MessageOutput | None
    error: SafeErrorOutput | None


class InputAttachmentOutput(BaseModel):
    name: str
    size: int
    sha256: str


class SavedAttachmentOutput(BaseModel):
    saved_name: str
    original_name: str
    content_type: str
    size: int
    sha256: str
    saved_path: str | None = None


class HtmlOutput(BaseModel):
    identity: IdentityOutput
    html: str
    flags: list[str]


class FlagChangeOutput(BaseModel):
    identity: IdentityOutput
    state: str
    enabled: bool
    changed: bool
    old_flags: list[str]
    new_flags: list[str]


class BatchFlagChangeOutput(BaseModel):
    identity: IdentityOutput
    ok: bool
    change: FlagChangeOutput | None
    error: SafeErrorOutput | None


class MoveOutput(BaseModel):
    identity: IdentityOutput
    destination_mailbox: str
    destination_identity: IdentityOutput | None
    method: str


class BatchMoveOutput(BaseModel):
    identity: IdentityOutput
    ok: bool
    move: MoveOutput | None
    error: SafeErrorOutput | None


class DraftCreationOutput(BaseModel):
    account_id: str
    mailbox: str
    uid_validity: str | None
    uid: str | None
    message_id: str
    attachment_hashes: list[str]
    draft_id: str | None


class DraftUpdateOutput(BaseModel):
    account_id: str
    draft_id: str
    mailbox: str
    uid_validity: str | None
    uid: str | None
    message_id: str
    attachment_hashes: list[str]
    method: str
