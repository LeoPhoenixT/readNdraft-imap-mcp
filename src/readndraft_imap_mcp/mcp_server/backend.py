from __future__ import annotations

from typing import Protocol

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
)


class ReadOnlyBroker(Protocol):
    """Semantic broker boundary available to the untrusted MCP frontend."""

    def list_accounts(self) -> list[dict[str, str | int | bool | None]]: ...

    async def list_mailboxes(self, account_id: str) -> tuple[Mailbox, ...]: ...

    async def search_emails(
        self,
        account_id: str,
        mailbox: str,
        filters: SearchFilters,
        limit: int = 50,
    ) -> tuple[SearchResult, ...]: ...

    async def search_email_targets(
        self,
        targets: tuple[tuple[str, str], ...],
        filters: SearchFilters,
        limit: int = 50,
        cursor: str | None = None,
    ) -> SearchPage: ...

    async def get_email(self, identity: MessageIdentity) -> MessageContent: ...

    async def get_emails(
        self, identities: tuple[MessageIdentity, ...]
    ) -> tuple[BatchMessageContent, ...]: ...

    async def get_email_html(self, identity: MessageIdentity) -> HtmlContent: ...

    def list_attachment_inputs(self): ...

    async def save_attachment(
        self, identity: MessageIdentity, attachment_id: str
    ): ...

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
    ) -> DraftCreationResult: ...

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
    ) -> DraftUpdateResult: ...

    async def set_star(
        self,
        identity: MessageIdentity,
        starred: bool,
        client_id: str | None = None,
    ) -> FlagChange: ...

    async def set_read_state(
        self,
        identity: MessageIdentity,
        read: bool,
        client_id: str | None = None,
    ) -> FlagChange: ...

    async def set_read_state_batch(
        self,
        identities: tuple[MessageIdentity, ...],
        read: bool,
        client_id: str | None = None,
    ) -> tuple[BatchFlagChange, ...]: ...

    async def set_star_batch(
        self,
        identities: tuple[MessageIdentity, ...],
        starred: bool,
        client_id: str | None = None,
    ) -> tuple[BatchFlagChange, ...]: ...

    async def move_email(
        self,
        identity: MessageIdentity,
        destination_mailbox: str,
        client_id: str | None = None,
    ) -> MoveResult: ...

    async def move_emails_batch(
        self,
        identities: tuple[MessageIdentity, ...],
        destination_mailbox: str,
        client_id: str | None = None,
    ) -> tuple[BatchMoveResult, ...]: ...


class UnavailableBroker:
    """Fail-closed placeholder until the local broker transport is wired."""

    def _unavailable(self):
        raise RuntimeError("local read-only broker connection is not configured")

    def list_accounts(self):
        return self._unavailable()

    async def list_mailboxes(self, account_id: str):
        return self._unavailable()

    async def search_emails(
        self, account_id, mailbox, filters, limit=50
    ):
        return self._unavailable()

    async def search_email_targets(
        self, targets, filters, limit=50
    ):
        return self._unavailable()

    async def get_email(self, identity):
        return self._unavailable()

    async def get_emails(self, identities):
        return self._unavailable()

    async def get_email_html(self, identity):
        return self._unavailable()

    def list_attachment_inputs(self):
        return self._unavailable()

    async def save_attachment(self, identity, attachment_id):
        return self._unavailable()

    async def create_draft(self, account_id, **kwargs):
        return self._unavailable()

    async def update_draft(self, account_id, draft_id, **kwargs):
        return self._unavailable()

    async def set_star(self, identity, starred, client_id=None):
        return self._unavailable()

    async def set_read_state(self, identity, read, client_id=None):
        return self._unavailable()

    async def set_read_state_batch(
        self, identities, read, client_id=None
    ):
        return self._unavailable()

    async def set_star_batch(self, identities, starred, client_id=None):
        return self._unavailable()

    async def move_email(self, identity, destination_mailbox, client_id=None):
        return self._unavailable()

    async def move_emails_batch(
        self, identities, destination_mailbox, client_id=None
    ):
        return self._unavailable()
