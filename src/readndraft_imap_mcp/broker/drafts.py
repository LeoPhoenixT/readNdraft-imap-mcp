"""Draft creation, update, recovery, and provenance broker operations."""

from __future__ import annotations

import secrets
from dataclasses import replace
from time import perf_counter
from typing import TYPE_CHECKING, Protocol, TypeVar

from readndraft_imap_mcp.attachments import AttachmentExchange
from readndraft_imap_mcp.audit import AuditEvent, AuditSink, AuditUnavailableError
from readndraft_imap_mcp.drafts import (
    DraftProvenanceError,
    DraftRecoveryRequiredError,
    FileDraftStore,
)
from readndraft_imap_mcp.imap.client import ImapClient
from readndraft_imap_mcp.imap.models import DraftCreationResult, DraftUpdateResult, MessageIdentity
from readndraft_imap_mcp.mime.drafts import DraftAttachment, PreparedDraft, build_draft_message, prepare_draft

if TYPE_CHECKING:
    from collections.abc import Callable

    from readndraft_imap_mcp.broker.accounts import AccountConfig, AccountRegistry

T = TypeVar("T")


class _Execution(Protocol):
    def _current_accounts(self) -> AccountRegistry: ...

    async def _client_call(
        self, account_id: str, operation: Callable[[ImapClient], T], *, response_timeout: bool = True,
    ) -> T: ...


def _message_ids(value: str, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 8 * 1024:
        raise ValueError(f"invalid {field}")
    import re
    tokens = tuple(re.findall(r"<[^<>\s@]+@[^<>\s@]+>", value))
    if not tokens or re.sub(r"<[^<>\s@]+@[^<>\s@]+>", "", value).strip():
        raise ValueError(f"invalid {field}")
    return tokens


def reply_thread(source_id: str, references: str | None) -> tuple[str, tuple[str, ...]]:
    source = _message_ids(source_id, field="source Message-ID")
    if len(source) != 1:
        raise ValueError("invalid source Message-ID")
    items = _message_ids(references, field="source References") if references else ()
    normalized = tuple(dict.fromkeys((*items, source[0])))
    if len(normalized) > 100 or len(" ".join(normalized).encode("utf-8")) > 8 * 1024:
        raise ValueError("reply threading metadata exceeds limit")
    return source[0], normalized


class DraftService:
    def __init__(
        self, execution: _Execution, audit: AuditSink | None, drafts: FileDraftStore | None,
        attachments: AttachmentExchange | None,
    ) -> None:
        self._execution = execution
        self._audit = audit
        self._drafts = drafts
        self._attachments = attachments

    def _prepare_attachments(self, attachment_names: tuple[str, ...]) -> tuple[DraftAttachment, ...]:
        if self._attachments is None:
            if attachment_names:
                raise RuntimeError("attachment exchange is not configured")
            return ()
        return tuple(
            DraftAttachment(item.filename, item.size, item.sha256, item.content)
            for item in self._attachments.prepare(attachment_names)
        )

    def _build_draft(
        self,
        account: AccountConfig,
        *,
        to: tuple[str, ...],
        cc: tuple[str, ...],
        bcc: tuple[str, ...],
        subject: str,
        body: str,
        html_body: str | None,
        attachment_names: tuple[str, ...],
        in_reply_to: str | None,
        references: tuple[str, ...],
        message_id: str | None = None,
        operation_id: str | None = None,
        prepared: PreparedDraft | None = None,
    ) -> tuple[bytes, str, PreparedDraft]:
        draft = (
            prepare_draft(
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                body=body,
                html_body=html_body,
                attachments=self._prepare_attachments(attachment_names),
                in_reply_to=in_reply_to,
                references=references,
            )
            if prepared is None
            else replace(prepared, in_reply_to=in_reply_to, references=references)
        )
        raw, message_id = build_draft_message(
            account.effective_sender_address,
            draft,
            sender_name=account.sender_name,
            message_id=message_id,
            operation_id=operation_id,
        )
        return raw, message_id, draft

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
        if self._audit is None:
            raise AuditUnavailableError("audit sink is required for draft creation")
        if self._drafts is None:
            raise RuntimeError("draft provenance store is required for draft creation")
        account = self._execution._current_accounts().require_enabled(account_id)
        if reply_to_message is not None and reply_to_message.account_id != account_id:
            raise PermissionError("reply source belongs to another account")
        started = perf_counter()
        stage = "draft_build"
        raw: bytes | None = None
        mailbox = ""
        uid = ""
        in_reply_to: str | None = None
        references: tuple[str, ...] = ()
        try:
            # Validate every caller-controlled draft field and attachment before
            # task admission. Reply metadata is added only after this succeeds.
            draft = prepare_draft(
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                body=body,
                html_body=html_body,
                attachments=self._prepare_attachments(attachment_names),
            )
            if reply_to_message is not None:
                stage = "threading_lookup"
                source_id, source_references = await self._execution._client_call(
                    account_id, lambda client: client.get_threading_headers(reply_to_message)
                )
                in_reply_to, references = reply_thread(source_id, source_references)
            stage = "draft_build"
            raw, message_id, draft = self._build_draft(
                account,
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                body=body,
                html_body=html_body,
                attachment_names=attachment_names,
                in_reply_to=in_reply_to,
                references=references,
                prepared=draft,
            )
            stage = "imap_append"
            result = await self._execution._client_call(
                account_id,
                lambda client: client.append_draft(
                    raw,
                    message_id,
                    tuple(item.sha256 for item in draft.attachments),
                ),
                response_timeout=False,
            )
            mailbox = result.mailbox
            uid = result.uid or ""
            stage = "provenance_write"
            provenance = self._drafts.create(
                account_id=result.account_id,
                mailbox=result.mailbox,
                uid_validity=result.uid_validity,
                uid=result.uid,
                message_id=result.message_id,
                attachment_hashes=result.attachment_hashes,
                in_reply_to=in_reply_to,
                references=references,
            )
        except Exception as exc:
            await self._audit.record(
                AuditEvent.draft_creation(
                    account_id=account_id,
                    mailbox=mailbox,
                    uid=uid,
                    request_size=len(raw) if raw is not None else 0,
                    success=False,
                    duration_ms=int((perf_counter() - started) * 1000),
                    error_category=type(exc).__name__,
                    client_id=client_id,
                    stage=stage,
                )
            )
            raise
        await self._audit.record(
            AuditEvent.draft_creation(
                account_id=account_id,
                mailbox=result.mailbox,
                uid=result.uid or "",
                request_size=len(raw),
                success=True,
                duration_ms=int((perf_counter() - started) * 1000),
                client_id=client_id,
            )
        )
        return replace(result, draft_id=provenance.draft_id)

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
        if self._audit is None:
            raise AuditUnavailableError("audit sink is required for draft updates")
        if self._drafts is None:
            raise RuntimeError("draft provenance store is required for draft updates")
        account = self._execution._current_accounts().require_enabled(account_id)
        # A non-blocking filesystem lock makes concurrent frontends and the
        # maintenance CLI fail before any IMAP write rather than serialising a
        # stale request behind an update.
        with self._drafts.operation_lock(draft_id):
            record = self._drafts.get(draft_id, account_id)
            if not record.update_supported:
                raise RuntimeError("draft update is unsupported without stable APPENDUID provenance")
            operation = self._drafts.get_operation(draft_id)
            if operation is not None:
                recovered_hashes = self._drafts.validate_operation(record, operation)
                try:
                    matches = await self._execution._client_call(
                        account_id,
                        lambda client: client.resolve_draft_operation(record, operation["operation_id"]),
                        response_timeout=False,
                    )
                except AttributeError as exc:
                    raise DraftRecoveryRequiredError("draft update recovery is required") from exc
                if len(matches) == 1 and operation.get("new_uid", matches[0]) == matches[0]:
                    old_uid = operation["old_uid"]
                    record = self._drafts.update(
                        record,
                        mailbox=operation["mailbox"],
                        uid_validity=operation["uid_validity"],
                        uid=matches[0],
                        message_id=record.message_id,
                        attachment_hashes=recovered_hashes,
                        superseded_uid=old_uid,
                    )
                    await self._execution._client_call(
                        account_id,
                        lambda client: client.expunge_superseded_draft(record, old_uid),
                        response_timeout=False,
                    )
                    record = self._drafts.update(
                        record,
                        mailbox=record.mailbox,
                        uid_validity=record.uid_validity,
                        uid=record.uid,
                        message_id=record.message_id,
                        attachment_hashes=recovered_hashes,
                        superseded_uid=None,
                    )
                    self._drafts.clear_operation(draft_id)
                else:
                    raise DraftRecoveryRequiredError("draft update recovery is required")
            started = perf_counter()
            stage = "draft_build"
            raw: bytes | None = None
            try:
                operation_id = secrets.token_hex(16)
                raw, message_id, draft = self._build_draft(
                    account,
                    to=to,
                    cc=cc,
                    bcc=bcc,
                    subject=subject,
                    body=body,
                    html_body=html_body,
                    attachment_names=attachment_names,
                    in_reply_to=record.in_reply_to,
                    references=record.references,
                    message_id=record.message_id,
                    operation_id=operation_id,
                )
                stage = "imap_append"
                matches = await self._execution._client_call(
                    account_id,
                    lambda client: client.resolve_draft_uid(record),
                    response_timeout=False,
                )
                if matches != (record.uid,):
                    if len(matches) != 1:
                        detail = "no matching message" if not matches else f"{len(matches)} matching messages"
                        raise DraftProvenanceError(
                            f"draft tracking record is stale ({detail}); run "
                            f"readndraft-imap-mcp drafts repair --draft-id {draft_id}"
                        )
                    record = self._drafts.update(
                        record,
                        mailbox=record.mailbox,
                        uid_validity=record.uid_validity,
                        uid=matches[0],
                        message_id=record.message_id,
                        attachment_hashes=record.attachment_hashes,
                        superseded_uid=record.superseded_uid,
                    )
                if record.superseded_uid is not None:
                    await self._execution._client_call(
                        account_id,
                        lambda client: client.expunge_superseded_draft(record, record.superseded_uid or ""),
                        response_timeout=False,
                    )
                    record = self._drafts.update(
                        record,
                        mailbox=record.mailbox,
                        uid_validity=record.uid_validity,
                        uid=record.uid,
                        message_id=record.message_id,
                        attachment_hashes=record.attachment_hashes,
                        superseded_uid=None,
                    )
                # The journal is committed before APPEND. It contains only
                # provenance identities, attachment hashes, and the random
                # operation marker, never mail text.
                self._drafts.write_operation(
                    draft_id,
                    {
                        "v": 2,
                        "operation_id": operation_id,
                        "account_id": account_id,
                        "mailbox": record.mailbox,
                        "uid_validity": record.uid_validity,
                        "old_uid": record.uid,
                        "message_id": message_id,
                        "attachment_hashes": [item.sha256 for item in draft.attachments],
                        "phase": "prepared",
                    },
                )
                result = await self._execution._client_call(
                    account_id,
                    lambda client: client.append_draft_update(
                        record,
                        raw,
                        message_id,
                        tuple(item.sha256 for item in draft.attachments),
                    ),
                    response_timeout=False,
                )
                old_uid = record.uid
                stage = "provenance_write"
                self._drafts.write_operation(
                    draft_id,
                    {
                        "v": 2,
                        "operation_id": operation_id,
                        "account_id": account_id,
                        "mailbox": result.mailbox,
                        "uid_validity": result.uid_validity,
                        "old_uid": old_uid,
                        "new_uid": result.uid,
                        "message_id": result.message_id,
                        "attachment_hashes": list(result.attachment_hashes),
                        "phase": "appended",
                    },
                )
                record = self._drafts.update(
                    record,
                    mailbox=result.mailbox,
                    uid_validity=result.uid_validity,
                    uid=result.uid,
                    message_id=result.message_id,
                    attachment_hashes=result.attachment_hashes,
                    superseded_uid=old_uid,
                )
                assert old_uid is not None
                await self._execution._client_call(
                    account_id,
                    lambda client: client.expunge_superseded_draft(record, old_uid),
                    response_timeout=False,
                )
                self._drafts.update(
                    record,
                    mailbox=result.mailbox,
                    uid_validity=result.uid_validity,
                    uid=result.uid,
                    message_id=result.message_id,
                    attachment_hashes=result.attachment_hashes,
                    superseded_uid=None,
                )
                self._drafts.clear_operation(draft_id)
            except Exception as exc:
                await self._audit.record(
                    AuditEvent.draft_update(
                        account_id=account_id,
                        mailbox=record.mailbox,
                        uid=record.uid or "",
                        request_size=len(raw) if raw is not None else 0,
                        success=False,
                        duration_ms=int((perf_counter() - started) * 1000),
                        error_category=type(exc).__name__,
                        client_id=client_id,
                        stage=stage,
                    )
                )
                raise
            await self._audit.record(
                AuditEvent.draft_update(
                    account_id=account_id,
                    mailbox=result.mailbox,
                    uid=result.uid or "",
                    request_size=len(raw),
                    success=True,
                    duration_ms=int((perf_counter() - started) * 1000),
                    client_id=client_id,
                )
            )
            return result
