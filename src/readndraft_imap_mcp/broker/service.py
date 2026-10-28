from __future__ import annotations

from collections.abc import Callable
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
from .common import BatchItemOutcome
from .drafts import reply_thread
from .execution import BrokerExecutionContext as BrokerExecutionContext
from .limits import AccountRequestQuota
from .mutations import mutation_spec

T = TypeVar("T")
Item = TypeVar("Item")

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
        write: bool = False,
    ) -> tuple[BatchItemOutcome[T], ...]:
        return await self._execution._batch_client_call(
            account_id,
            items,
            operation,
            max_items=max_items,
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
