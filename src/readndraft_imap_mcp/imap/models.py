from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal


@dataclass(frozen=True, slots=True)
class Mailbox:
    name: str
    delimiter: str | None
    flags: tuple[str, ...]
    display_name: str | None = None

    def __post_init__(self) -> None:
        if self.display_name is None:
            object.__setattr__(self, "display_name", self.name)


@dataclass(frozen=True, slots=True)
class MessageIdentity:
    account_id: str
    mailbox: str
    uid_validity: str
    uid: str

    def __post_init__(self) -> None:
        if not self.account_id or not self.mailbox:
            raise ValueError("account_id and mailbox are required")
        if not self.uid_validity.isascii() or not self.uid_validity.isdigit():
            raise ValueError("uid_validity must be numeric")
        if not self.uid.isascii() or not self.uid.isdigit():
            raise ValueError("uid must be numeric")


@dataclass(frozen=True, slots=True)
class SearchFilters:
    sender: str | None = None
    recipient: str | None = None
    subject: str | None = None
    text: str | None = None
    attachment_filename: str | None = None
    after: date | None = None
    before: date | None = None
    read: bool | None = None
    starred: bool | None = None


@dataclass(frozen=True, slots=True)
class SearchResult:
    identity: MessageIdentity
    headers: dict[str, str]
    flags: tuple[str, ...]
    size: int
    received_at: str = ""


@dataclass(frozen=True, slots=True)
class SearchWindow:
    results: tuple[SearchResult, ...]
    uid_validity: str
    next_uid: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class SearchTargetError:
    account_id: str
    mailbox: str
    error: str


@dataclass(frozen=True, slots=True)
class SearchTarget:
    account_id: str
    mailbox: str


@dataclass(frozen=True, slots=True)
class SearchPage:
    results: tuple[SearchResult, ...]
    errors: tuple[SearchTargetError, ...]
    next_cursor: str | None
    truncated: bool
    order: str
    targets_searched: tuple[SearchTarget, ...] = ()
    targets_pending: tuple[SearchTarget, ...] = ()


@dataclass(frozen=True, slots=True)
class AttachmentMetadata:
    attachment_id: str
    filename: str
    content_type: str
    size: int


@dataclass(frozen=True, slots=True)
class MessageContent:
    identity: MessageIdentity
    headers: dict[str, str]
    text: str
    flags: tuple[str, ...]
    attachments: tuple[AttachmentMetadata, ...]
    source_size: int = 0


@dataclass(frozen=True, slots=True)
class HtmlContent:
    identity: MessageIdentity
    html: str
    flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DraftCreationResult:
    account_id: str
    mailbox: str
    uid_validity: str | None
    uid: str | None
    message_id: str
    attachment_hashes: tuple[str, ...]
    draft_id: str | None = None


@dataclass(frozen=True, slots=True)
class DraftUpdateResult:
    account_id: str
    draft_id: str
    mailbox: str
    uid_validity: str | None
    uid: str | None
    message_id: str
    attachment_hashes: tuple[str, ...]
    method: Literal["replace", "uidplus"]


@dataclass(frozen=True, slots=True)
class AttachmentContent:
    metadata: AttachmentMetadata
    content: bytes


@dataclass(frozen=True, slots=True)
class FlagChange:
    identity: MessageIdentity
    state: Literal["starred", "read"]
    enabled: bool
    changed: bool
    old_flags: tuple[str, ...]
    new_flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BatchFlagChange:
    identity: MessageIdentity
    ok: bool
    change: FlagChange | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.ok != (self.change is not None and self.error is None):
            raise ValueError("batch flag result must contain either a change or an error")


@dataclass(frozen=True, slots=True)
class BatchMessageContent:
    identity: MessageIdentity
    ok: bool
    message: MessageContent | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.ok != (self.message is not None and self.error is None):
            raise ValueError("batch message result must contain either a message or an error")
