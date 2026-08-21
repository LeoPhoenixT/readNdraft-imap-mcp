from __future__ import annotations

import asyncio
from email import policy
from email.parser import BytesParser

from readndraft_imap_mcp.broker import AccountConfig, AccountRegistry, BrokerService
from readndraft_imap_mcp.drafts import FileDraftStore
from readndraft_imap_mcp.imap.models import (
    DraftCreationResult,
    DraftUpdateResult,
    FlagChange,
    HtmlContent,
    MessageIdentity,
    SearchFilters,
)


IDENTITY = MessageIdentity("personal", "INBOX", "42", "7")


class Credentials:
    async def load_secret(self, account_id: str) -> str:
        return "secret"


class Client:
    draft_senders = []
    draft_messages = []

    def __init__(self, account, secret):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def search(self, mailbox, filters, limit):
        return ()

    def get_html(self, identity):
        return HtmlContent(identity, "<p>safe</p>", ())

    def append_draft(self, raw, message_id, attachment_hashes):
        message = BytesParser(policy=policy.default).parsebytes(raw)
        self.draft_senders.append(message["From"])
        self.draft_messages.append(message)
        return DraftCreationResult(
            "personal", "Drafts", "42", "99", message_id, attachment_hashes
        )

    def resolve_draft_uid(self, record):
        return (record.uid,)

    def append_draft_update(self, record, raw, message_id, attachment_hashes):
        message = BytesParser(policy=policy.default).parsebytes(raw)
        self.draft_senders.append(message["From"])
        self.draft_messages.append(message)
        return DraftUpdateResult(
            "personal",
            record.draft_id,
            "Drafts",
            "42",
            "100",
            message_id,
            attachment_hashes,
            "replace",
        )

    def expunge_superseded_draft(self, record, uid):
        return None

    def set_read_state(self, identity, read):
        return FlagChange(identity, "read", read, True, (), (r"\Seen",))


class Audit:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)


def build_broker(
    tmp_path, audit=None, *, sender_address=None, sender_name=None
) -> BrokerService:
    return BrokerService(
        AccountRegistry(
            [
                AccountConfig(
                    "personal",
                    "pinned.example.com",
                    993,
                    "login@internal.example",
                    sender_address=sender_address,
                    sender_name=sender_name,
                )
            ]
        ),
        Credentials(),
        Client,
        audit=audit,
        drafts=FileDraftStore((tmp_path / "drafts").resolve()),
    )


def test_large_search_and_html_read_have_no_runtime_approval(tmp_path) -> None:
    broker = build_broker(tmp_path)
    assert asyncio.run(
        broker.search_emails("personal", "INBOX", SearchFilters(), 500)
    ) == ()
    assert asyncio.run(broker.get_email_html(IDENTITY)).html == "<p>safe</p>"


def test_draft_creation_and_update_have_no_runtime_approval(tmp_path) -> None:
    audit = Audit()
    broker = build_broker(tmp_path, audit)
    created = asyncio.run(
        broker.create_draft(
            "personal",
            to=("recipient@example.com",),
            subject="draft",
            body="version one",
        )
    )
    updated = asyncio.run(
        broker.update_draft(
            "personal",
            created.draft_id,
            to=("recipient@example.com",),
            subject="draft updated",
            body="version two",
        )
    )

    assert updated.uid == "100"
    assert [event.operation for event in audit.events] == [
        "create_draft",
        "update_draft",
    ]
    assert all(event.approval_required is False for event in audit.events)


def test_broker_adds_updates_and_removes_html_without_changing_message_id(tmp_path) -> None:
    Client.draft_messages = []
    broker = build_broker(tmp_path, Audit())
    created = asyncio.run(
        broker.create_draft(
            "personal", to=("recipient@example.com",), subject="draft",
            body="plain one", html_body="<p><strong>rich one</strong></p>",
        )
    )
    asyncio.run(
        broker.update_draft(
            "personal", created.draft_id, to=("recipient@example.com",),
            subject="draft", body="plain two", html_body="<p>rich two</p>",
        )
    )
    asyncio.run(
        broker.update_draft(
            "personal", created.draft_id, to=("recipient@example.com",),
            subject="draft", body="plain three", html_body=None,
        )
    )

    assert [item.get_content_type() for item in Client.draft_messages] == [
        "multipart/alternative", "multipart/alternative", "text/plain",
    ]
    assert len({str(item["Message-ID"]) for item in Client.draft_messages}) == 1


def test_drafts_use_pinned_sender_for_create_and_update(tmp_path) -> None:
    Client.draft_senders = []
    broker = build_broker(tmp_path, Audit(), sender_address="leo@example.com")
    created = asyncio.run(
        broker.create_draft(
            "personal", to=("recipient@example.com",), subject="one", body="body"
        )
    )
    asyncio.run(
        broker.update_draft(
            "personal",
            created.draft_id,
            to=("recipient@example.com",),
            subject="two",
            body="body",
        )
    )
    assert Client.draft_senders == ["leo@example.com", "leo@example.com"]


def test_drafts_use_pinned_sender_name_for_create_and_update(tmp_path) -> None:
    Client.draft_messages = []
    broker = build_broker(
        tmp_path,
        Audit(),
        sender_address="user@example.com",
        sender_name="山田太郎",
    )
    created = asyncio.run(
        broker.create_draft(
            "personal", to=("recipient@example.com",), subject="one", body="body"
        )
    )
    asyncio.run(
        broker.update_draft(
            "personal", created.draft_id, to=("recipient@example.com",),
            subject="two", body="body",
        )
    )
    for message in Client.draft_messages:
        sender = message["From"].addresses[0]
        assert sender.display_name == "山田太郎"
        assert sender.addr_spec == "user@example.com"


def test_large_mutation_batch_has_no_runtime_approval(tmp_path) -> None:
    audit = Audit()
    broker = build_broker(tmp_path, audit)
    identities = tuple(
        MessageIdentity("personal", "INBOX", "42", str(uid))
        for uid in range(1, 51)
    )

    results = asyncio.run(broker.set_read_state_batch(identities, True))

    assert len(results) == 50
    assert all(item.ok for item in results)
    assert len(audit.events) == 50
    assert all(event.approval_required is False for event in audit.events)


def test_public_mcp_schema_has_no_approval_id() -> None:
    from readndraft_imap_mcp.mcp_server.server import create_server
    from test_mcp_server import FakeBroker

    schemas = repr(
        [tool.parameters for tool in create_server(FakeBroker())._tool_manager._tools.values()]
    )
    assert "approval_id" not in schemas
