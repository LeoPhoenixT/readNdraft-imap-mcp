from __future__ import annotations

import asyncio

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
        return DraftCreationResult(
            "personal", "Drafts", "42", "99", message_id, attachment_hashes
        )

    def replace_draft(self, record, raw, message_id, attachment_hashes):
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

    def set_read_state(self, identity, read):
        return FlagChange(identity, "read", read, True, (), (r"\Seen",))


class Audit:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)


def build_broker(tmp_path, audit=None) -> BrokerService:
    return BrokerService(
        AccountRegistry(
            [AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]
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
