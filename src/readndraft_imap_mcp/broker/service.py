from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from time import perf_counter
from typing import Generic, TypeVar

from readndraft_imap_mcp.attachments import AttachmentExchange, InputAttachment, SavedAttachment
from readndraft_imap_mcp.audit import AuditEvent, AuditSink, AuditUnavailableError
from readndraft_imap_mcp.credentials import CredentialStore
from readndraft_imap_mcp.drafts import DraftProvenanceError, FileDraftStore
from readndraft_imap_mcp.imap.client import (
    ImapClient,
    ImapClientError,
    ImapMovePartialError,
)
from readndraft_imap_mcp.imap.models import (
    BatchFlagChange,
    BatchMessageContent,
    BatchMoveResult,
    DraftCreationResult,
    DraftUpdateResult,
    FlagChange,
    HtmlContent,
    Mailbox,
    MessageContent,
    MessageIdentity,
    MoveResult,
    SearchFilters,
    SearchPage,
    SearchResult,
    SearchTarget,
    SearchTargetError,
)
from readndraft_imap_mcp.mime.drafts import (
    DraftAttachment,
    PreparedDraft,
    build_draft_message,
    prepare_draft,
)
from readndraft_imap_mcp.mime.parser import MAX_MESSAGE_BYTES, MAX_TEXT_BYTES

from .accounts import AccountConfig, AccountRegistry
from .limits import AccountRequestQuota, RequestQuotaError
from .protocol import HealthResponse, decode_request

T = TypeVar("T")
Item = TypeVar("Item")
_MESSAGE_ID_RE = re.compile(r"<[^<>\s@]+@[^<>\s@]+>")
MAX_THREAD_IDS = 100
MAX_THREAD_BYTES = 8 * 1024
_MUTATION_OPERATIONS = {
    "set_star": ("set_star", r"\Flagged"),
    "set_read_state": ("set_read_state", r"\Seen"),
}


def _mutation_spec(operation: str) -> tuple[str, str]:
    try:
        return _MUTATION_OPERATIONS[operation]
    except KeyError as exc:
        raise ValueError(f"unsupported mutation operation: {operation}") from exc


def _message_ids(value: str, *, field: str) -> tuple[str, ...]:
    """Accept only a whitespace-separated RFC 5322 message-id token sequence."""
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_THREAD_BYTES:
        raise ValueError(f"invalid {field}")
    tokens = tuple(_MESSAGE_ID_RE.findall(value))
    if not tokens or _MESSAGE_ID_RE.sub("", value).strip():
        raise ValueError(f"invalid {field}")
    return tokens


def _reply_thread(source_id: str, references: str | None) -> tuple[str, tuple[str, ...]]:
    source = _message_ids(source_id, field="source Message-ID")
    if len(source) != 1:
        raise ValueError("invalid source Message-ID")
    items = _message_ids(references, field="source References") if references else ()
    normalized = tuple(dict.fromkeys((*items, source[0])))
    if len(normalized) > MAX_THREAD_IDS or len(" ".join(normalized).encode("utf-8")) > MAX_THREAD_BYTES:
        raise ValueError("reply threading metadata exceeds limit")
    return source[0], normalized


@dataclass(frozen=True, slots=True)
class BatchItemOutcome(Generic[T]):
    """One bounded batch result with a deliberately non-sensitive error category."""

    value: T | None = None
    error: str | None = None


def _batch_error(exc: Exception) -> str:
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
    if isinstance(exc, ImapClientError):
        return "imap_error"
    if isinstance(exc, OSError):
        return "connection_error"
    return "broker_error"


def _search_fingerprint(filters: SearchFilters) -> str:
    values = asdict(filters)
    values["after"] = filters.after.isoformat() if filters.after else None
    values["before"] = filters.before.isoformat() if filters.before else None
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _encode_search_cursor(
    account_id: str,
    mailbox: str,
    uid_validity: str,
    before_uid: str,
    filters: SearchFilters,
) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "account_id": account_id,
            "mailbox": mailbox,
            "uid_validity": uid_validity,
            "before_uid": before_uid,
            "filters": _search_fingerprint(filters),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_search_cursor(
    value: str,
    account_id: str,
    mailbox: str,
    filters: SearchFilters,
) -> tuple[str, str]:
    if not value or len(value) > 2048 or not value.isascii():
        raise ValueError("invalid search cursor")
    try:
        encoded = value.encode("ascii")
        payload = json.loads(
            base64.b64decode(encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid search cursor") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "v",
        "account_id",
        "mailbox",
        "uid_validity",
        "before_uid",
        "filters",
    }:
        raise ValueError("invalid search cursor")
    if (
        payload["v"] != 1
        or payload["account_id"] != account_id
        or payload["mailbox"] != mailbox
        or payload["filters"] != _search_fingerprint(filters)
        or not isinstance(payload["uid_validity"], str)
        or not isinstance(payload["before_uid"], str)
        or not payload["uid_validity"].isdigit()
        or not payload["before_uid"].isdigit()
    ):
        raise ValueError("search cursor does not match this query")
    return payload["uid_validity"], payload["before_uid"]


class BrokerService:
    """Capability-minimized broker with Phase 2 read-only operations."""

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
    ) -> None:
        self._accounts = accounts or AccountRegistry(())
        self._accounts_loader = accounts_loader
        self._credentials = credentials
        self._client_factory = client_factory
        self._audit = audit
        self._drafts = drafts
        self._attachments = attachments
        self._quota = quota or AccountRequestQuota()
        if request_timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        self._request_timeout_seconds = request_timeout_seconds

    def handle(self, payload: object) -> dict[str, str | bool]:
        decode_request(payload)
        return HealthResponse().to_dict()

    def list_accounts(self) -> list[dict[str, str | int | bool | None]]:
        return self._current_accounts().list_safe()

    def _current_accounts(self) -> AccountRegistry:
        return self._accounts_loader() if self._accounts_loader is not None else self._accounts

    async def _credential(self, account_id: str) -> tuple[AccountConfig, str]:
        account = self._current_accounts().require_enabled(account_id)
        if self._credentials is None:
            raise RuntimeError("credential store is not configured")
        secret = await asyncio.wait_for(
            self._credentials.load_secret(account_id), self._request_timeout_seconds
        )
        return account, secret

    async def _client_call(
        self,
        account_id: str,
        operation: Callable[[ImapClient], T],
        *,
        response_timeout: bool = True,
        quota_cost: int = 1,
    ) -> T:
        account, secret = await self._credential(account_id)

        def run() -> T:
            nonlocal secret
            try:
                with self._quota.slot(account_id, cost=quota_cost):
                    with self._client_factory(account, secret) as client:
                        return operation(client)
            finally:
                secret = ""

        pending = asyncio.to_thread(run)
        if response_timeout:
            return await asyncio.wait_for(pending, self._request_timeout_seconds)
        return await pending

    async def _batch_client_call(
        self,
        account_id: str,
        items: tuple[Item, ...],
        operation: Callable[[ImapClient, Item], T],
        *,
        max_items: int,
        response_timeout: bool = True,
    ) -> tuple[BatchItemOutcome[T], ...]:
        """Run a bounded account batch sequentially on one authenticated session."""
        if not items or len(items) > max_items:
            raise ValueError(f"batch must contain between 1 and {max_items} items")
        account, secret = await self._credential(account_id)

        def run() -> tuple[BatchItemOutcome[T], ...]:
            nonlocal secret
            try:
                with self._quota.slot(account_id, cost=len(items)):
                    with self._client_factory(account, secret) as client:
                        outcomes: list[BatchItemOutcome[T]] = []
                        for item in items:
                            try:
                                outcomes.append(BatchItemOutcome(value=operation(client, item)))
                            except Exception as exc:
                                outcomes.append(BatchItemOutcome(error=_batch_error(exc)))
                        return tuple(outcomes)
            finally:
                secret = ""

        pending = asyncio.to_thread(run)
        if response_timeout:
            return await asyncio.wait_for(pending, self._request_timeout_seconds)
        return await pending

    async def list_mailboxes(self, account_id: str) -> tuple[Mailbox, ...]:
        return await self._client_call(account_id, lambda client: client.list_mailboxes())

    async def search_emails(
        self,
        account_id: str,
        mailbox: str,
        filters: SearchFilters,
        limit: int = 50,
    ) -> tuple[SearchResult, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("search limit must be between 1 and 500")
        return await self._client_call(
            account_id, lambda client: client.search(mailbox, filters, limit)
        )

    async def search_email_targets(
        self,
        targets: tuple[tuple[str, str], ...],
        filters: SearchFilters,
        limit: int = 50,
        cursor: str | None = None,
    ) -> SearchPage:
        if not targets or len(targets) > 20:
            raise ValueError("search must contain between 1 and 20 targets")
        if len(set(targets)) != len(targets):
            raise ValueError("search targets must be unique")
        if not 1 <= limit <= 500:
            raise ValueError("search limit must be between 1 and 500")
        if limit > 50 and len(targets) != 1:
            raise ValueError("searches over 50 results require exactly one target")
        if cursor is not None and len(targets) != 1:
            raise ValueError("cursor pagination requires exactly one target")
        if len(targets) == 1:
            account_id, mailbox = targets[0]
            target = SearchTarget(account_id, mailbox)
            expected_uid_validity = None
            before_uid = None
            if cursor is not None:
                expected_uid_validity, before_uid = _decode_search_cursor(
                    cursor, account_id, mailbox, filters
                )
            try:
                window = await self._client_call(
                    account_id,
                    lambda client: client.search_window(
                        mailbox,
                        filters,
                        limit,
                        before_uid=before_uid,
                        expected_uid_validity=expected_uid_validity,
                    ),
                )
            except Exception as exc:
                return SearchPage(
                    results=(),
                    errors=(
                        SearchTargetError(account_id, mailbox, _batch_error(exc)),
                    ),
                    next_cursor=None,
                    truncated=False,
                    order="mailbox_uid_desc",
                    targets_searched=(target,),
                    targets_pending=(),
                )
            next_cursor = None
            if window.has_more and window.next_uid is not None:
                next_cursor = _encode_search_cursor(
                    account_id,
                    mailbox,
                    window.uid_validity,
                    window.next_uid,
                    filters,
                )
            return SearchPage(
                results=window.results,
                errors=(),
                next_cursor=next_cursor,
                truncated=window.has_more,
                order="mailbox_uid_desc",
                targets_searched=(target,),
                targets_pending=(),
            )

        grouped: dict[str, list[str]] = {}
        for account_id, mailbox in targets:
            grouped.setdefault(account_id, []).append(mailbox)
        results: list[SearchResult] = []
        errors: list[SearchTargetError] = []
        searched: list[SearchTarget] = []
        truncated = False
        for account_id, mailboxes in grouped.items():
            remaining = limit - len(results)
            if remaining == 0:
                truncated = True
                break

            def search_account(
                client: ImapClient,
            ) -> tuple[
                tuple[SearchResult, ...],
                tuple[SearchTargetError, ...],
                tuple[SearchTarget, ...],
                bool,
            ]:
                matches: list[SearchResult] = []
                failures: list[SearchTargetError] = []
                attempted: list[SearchTarget] = []
                page_truncated = False
                for index, mailbox in enumerate(mailboxes):
                    mailbox_remaining = remaining - len(matches)
                    if mailbox_remaining == 0:
                        page_truncated = True
                        break
                    attempted.append(SearchTarget(account_id, mailbox))
                    try:
                        window = client.search_window(
                            mailbox, filters, mailbox_remaining
                        )
                    except Exception as exc:
                        failures.append(
                            SearchTargetError(
                                account_id, mailbox, _batch_error(exc)
                            )
                        )
                        continue
                    matches.extend(window.results)
                    if window.has_more:
                        page_truncated = True
                    if len(matches) == remaining and index + 1 < len(mailboxes):
                        page_truncated = True
                return (
                    tuple(matches),
                    tuple(failures),
                    tuple(attempted),
                    page_truncated,
                )

            try:
                matches, failures, attempted, page_truncated = await self._client_call(
                    account_id,
                    search_account,
                    quota_cost=len(mailboxes),
                )
            except Exception as exc:
                category = _batch_error(exc)
                errors.extend(
                    SearchTargetError(account_id, mailbox, category)
                    for mailbox in mailboxes
                )
                searched.extend(SearchTarget(account_id, mailbox) for mailbox in mailboxes)
                continue
            results.extend(matches[:remaining])
            errors.extend(failures)
            searched.extend(attempted)
            truncated = truncated or page_truncated
        searched_set = set(searched)
        pending = tuple(
            SearchTarget(account_id, mailbox)
            for account_id, mailbox in targets
            if SearchTarget(account_id, mailbox) not in searched_set
        )
        return SearchPage(
            results=tuple(results),
            errors=tuple(errors),
            next_cursor=None,
            truncated=truncated or bool(pending),
            order="target_then_mailbox_uid_desc",
            targets_searched=tuple(searched),
            targets_pending=pending,
        )

    async def get_email(self, identity: MessageIdentity) -> MessageContent:
        return await self._client_call(
            identity.account_id, lambda client: client.get_message(identity)
        )

    async def get_emails(
        self, identities: tuple[MessageIdentity, ...]
    ) -> tuple[BatchMessageContent, ...]:
        if not identities or len(identities) > 10:
            raise ValueError("batch must contain between 1 and 10 identities")
        keys = [
            (item.account_id, item.mailbox, item.uid_validity, item.uid)
            for item in identities
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("batch identities must be unique")
        account_ids = tuple(dict.fromkeys(item.account_id for item in identities))
        if len(account_ids) > 2:
            raise ValueError("batch may span at most 2 accounts")
        indexed_groups: dict[str, list[tuple[int, MessageIdentity]]] = {}
        for index, identity in enumerate(identities):
            indexed_groups.setdefault(identity.account_id, []).append((index, identity))

        budget_lock = threading.Lock()
        remaining_source = [MAX_MESSAGE_BYTES]
        remaining_text = [MAX_TEXT_BYTES]

        async def run_account(account_id: str, indexed):
            def read(client: ImapClient, identity: MessageIdentity) -> MessageContent:
                # Serialize budget reservation across account worker threads. This
                # prevents the aggregate raw limit from being raced by two sessions.
                with budget_lock:
                    message = client.get_message(identity, remaining_source[0])
                    remaining_source[0] -= message.source_size
                    text_size = len(message.text.encode("utf-8"))
                    if text_size > remaining_text[0]:
                        raise ValueError("batch plain-text response exceeds 2 MB")
                    remaining_text[0] -= text_size
                    return message

            account_items = tuple(identity for _, identity in indexed)
            try:
                outcomes = await self._batch_client_call(
                    account_id, account_items, read, max_items=10
                )
            except Exception as exc:
                outcomes = tuple(
                    BatchItemOutcome[MessageContent](error=_batch_error(exc))
                    for _ in account_items
                )
            return indexed, outcomes

        completed = await asyncio.gather(
            *(run_account(account_id, indexed_groups[account_id]) for account_id in account_ids)
        )
        results: list[BatchMessageContent | None] = [None] * len(identities)
        for indexed, outcomes in completed:
            for (index, identity), outcome in zip(indexed, outcomes, strict=True):
                results[index] = BatchMessageContent(
                    identity=identity,
                    ok=outcome.value is not None,
                    message=outcome.value,
                    error=outcome.error,
                )
        return tuple(item for item in results if item is not None)

    async def get_email_html(self, identity: MessageIdentity) -> HtmlContent:
        return await self._client_call(
            identity.account_id, lambda client: client.get_html(identity)
        )

    def list_attachment_inputs(self) -> tuple[InputAttachment, ...]:
        if self._attachments is None:
            raise RuntimeError("attachment exchange is not configured")
        return self._attachments.list_inputs()

    async def save_attachment(
        self, identity: MessageIdentity, attachment_id: str
    ) -> SavedAttachment:
        if self._attachments is None:
            raise RuntimeError("attachment exchange is not configured")
        attachment = await self._client_call(
            identity.account_id,
            lambda client: client.get_attachment(identity, attachment_id),
        )
        return self._attachments.save(
            attachment.metadata.filename,
            attachment.metadata.content_type,
            attachment.content,
        )

    def _prepare_attachments(
        self, attachment_names: tuple[str, ...]
    ) -> tuple[DraftAttachment, ...]:
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
    ) -> tuple[bytes, str, PreparedDraft]:
        draft = prepare_draft(
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
        raw, message_id = build_draft_message(
            account.effective_sender_address,
            draft,
            sender_name=account.sender_name,
            message_id=message_id,
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
        account = self._current_accounts().require_enabled(account_id)
        if reply_to_message is not None and reply_to_message.account_id != account_id:
            raise PermissionError("reply source belongs to another account")
        in_reply_to: str | None = None
        references: tuple[str, ...] = ()
        if reply_to_message is not None:
            source_id, source_references = await self._client_call(
                account_id, lambda client: client.get_threading_headers(reply_to_message)
            )
            in_reply_to, references = _reply_thread(source_id, source_references)
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
        )
        started = perf_counter()
        try:
            result = await self._client_call(
                account_id,
                lambda client: client.append_draft(
                    raw,
                    message_id,
                    tuple(item.sha256 for item in draft.attachments),
                ),
                response_timeout=False,
            )
        except Exception as exc:
            await self._audit.record(
                AuditEvent.draft_creation(
                    account_id=account_id,
                    mailbox="",
                    uid="",
                    request_size=len(raw),
                    success=False,
                    duration_ms=int((perf_counter() - started) * 1000),
                    error_category=type(exc).__name__,
                    client_id=client_id,
                )
            )
            raise
        try:
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
                    mailbox=result.mailbox,
                    uid=result.uid or "",
                    request_size=len(raw),
                    success=False,
                    duration_ms=int((perf_counter() - started) * 1000),
                    error_category=type(exc).__name__,
                    client_id=client_id,
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
        account = self._current_accounts().require_enabled(account_id)
        record = self._drafts.get(draft_id, account_id)
        if not record.update_supported:
            raise RuntimeError("draft update is unsupported without stable APPENDUID provenance")
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
        )
        started = perf_counter()
        try:
            matches = await self._client_call(
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
                await self._client_call(
                    account_id,
                    lambda client: client.expunge_superseded_draft(
                        record, record.superseded_uid or ""
                    ),
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
            result = await self._client_call(
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
            await self._client_call(
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
        except Exception as exc:
            await self._audit.record(
                AuditEvent.draft_update(
                    account_id=account_id,
                    mailbox=record.mailbox,
                    uid=record.uid or "",
                    request_size=len(raw),
                    success=False,
                    duration_ms=int((perf_counter() - started) * 1000),
                    error_category=type(exc).__name__,
                    client_id=client_id,
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

    async def _mutate(
        self,
        identity: MessageIdentity,
        operation: str,
        enabled: bool,
        client_id: str | None,
    ) -> FlagChange:
        if self._audit is None:
            raise AuditUnavailableError("audit sink is required for mutations")
        method_name, state_flag = _mutation_spec(operation)
        started = perf_counter()
        try:
            def mutate(client: ImapClient) -> FlagChange:
                return getattr(client, method_name)(identity, enabled)

            result = await self._client_call(
                identity.account_id, mutate, response_timeout=False
            )
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
        method_name, state_flag = _mutation_spec(operation)
        if not identities or len(identities) > 50:
            raise ValueError("batch must contain between 1 and 50 identities")
        keys = [
            (item.account_id, item.mailbox, item.uid_validity, item.uid)
            for item in identities
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("batch identities must be unique")
        account_ids = tuple(dict.fromkeys(item.account_id for item in identities))
        if len(account_ids) > 3:
            raise ValueError("batch may span at most 3 accounts")
        indexed_groups: dict[str, list[tuple[int, MessageIdentity]]] = {}
        for index, identity in enumerate(identities):
            indexed_groups.setdefault(identity.account_id, []).append((index, identity))

        async def run_account(account_id: str, indexed):
            account_items = tuple(identity for _, identity in indexed)

            def mutate(client: ImapClient, identity: MessageIdentity) -> FlagChange:
                return getattr(client, method_name)(identity, enabled)

            started = perf_counter()
            try:
                outcomes = await self._batch_client_call(
                    account_id,
                    account_items,
                    mutate,
                    max_items=50,
                    response_timeout=False,
                )
            except Exception as exc:
                outcomes = tuple(
                    BatchItemOutcome[FlagChange](error=_batch_error(exc))
                    for _ in account_items
                )
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
                        error_category=outcome.error,
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
        return await self._batch_mutate(
            identities, "set_read_state", read, client_id
        )

    async def set_star_batch(
        self,
        identities: tuple[MessageIdentity, ...],
        starred: bool,
        client_id: str | None = None,
    ) -> tuple[BatchFlagChange, ...]:
        return await self._batch_mutate(
            identities, "set_star", starred, client_id
        )

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
            result = await self._client_call(
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
                    error_category=_batch_error(exc),
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
                destination_uid_validity=(
                    destination.uid_validity if destination is not None else None
                ),
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
        if not identities or len(identities) > 50:
            raise ValueError("batch must contain between 1 and 50 identities")
        keys = [
            (item.account_id, item.mailbox, item.uid_validity, item.uid)
            for item in identities
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("batch identities must be unique")
        account_ids = {item.account_id for item in identities}
        if len(account_ids) != 1:
            raise ValueError("move batch must belong to exactly one account")
        account_id = identities[0].account_id
        started = perf_counter()
        try:
            outcomes = await self._batch_client_call(
                account_id,
                identities,
                lambda client, identity: client.move_email(
                    identity, destination_mailbox
                ),
                max_items=50,
                response_timeout=False,
            )
        except Exception as exc:
            outcomes = tuple(
                BatchItemOutcome[MoveResult](error=_batch_error(exc))
                for _ in identities
            )
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
                    destination_uid_validity=(
                        destination.uid_validity if destination is not None else None
                    ),
                    destination_uid=(destination.uid if destination is not None else None),
                    movement_method=move.method if move is not None else None,
                    success=move is not None,
                    duration_ms=duration_ms,
                    error_category=outcome.error,
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
