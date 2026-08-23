from __future__ import annotations

import asyncio
from contextlib import contextmanager

from mcp.shared.memory import create_connected_server_and_client_session

from readndraft_imap_mcp.attachments import InputAttachment, SavedAttachment
from readndraft_imap_mcp.imap.models import (
    AttachmentMetadata,
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
    SearchPage,
    SearchResult,
    SearchTarget,
)
from readndraft_imap_mcp.mcp_server import server as server_module
from readndraft_imap_mcp.mcp_server.server import create_server

IDENTITY = MessageIdentity("personal", "INBOX", "42", "7")


class FakeBroker:
    def list_accounts(self):
        return [
            {
                "id": "personal",
                "username": "l***@example.com",
                "host": "imap.example.com",
                "port": 993,
                "enabled": True,
                "sender_address": "leo@example.com",
                "sender_name": "Leo Phoenix",
            }
        ]

    async def list_mailboxes(self, account_id):
        assert account_id == "personal"
        return (Mailbox("INBOX", "/", (r"\HasNoChildren",)),)

    async def search_emails(self, account_id, mailbox, filters, limit=50):
        assert (account_id, mailbox, limit) == ("personal", "INBOX", 1)
        return (SearchResult(IDENTITY, {"subject": "test"}, (), 123),)

    async def search_email_targets(self, targets, filters, limit=50, cursor=None):
        assert (targets, limit) == ((('personal', 'INBOX'),), 1)
        assert cursor is None
        return SearchPage(
            results=(
                SearchResult(
                    IDENTITY,
                    {"subject": "test", "to": "recipient@example.com"},
                    (),
                    123,
                    "2026-08-11T00:00:00Z",
                ),
            ),
            errors=(),
            next_cursor=None,
            truncated=False,
            order="mailbox_uid_desc",
            targets_searched=(SearchTarget("personal", "INBOX"),),
            targets_pending=(),
        )

    async def get_email(self, identity):
        assert identity == IDENTITY
        return MessageContent(
            IDENTITY,
            {"subject": "test"},
            "plain text",
            (),
            (AttachmentMetadata("part-2", "safe.txt", "text/plain", 3),),
        )

    async def get_emails(self, identities):
        assert identities == (IDENTITY,)
        return (BatchMessageContent(IDENTITY, True, await self.get_email(IDENTITY)),)

    async def get_email_html(self, identity):
        assert identity == IDENTITY
        return HtmlContent(IDENTITY, "<p>safe</p>", ())

    def list_attachment_inputs(self):
        return (InputAttachment("draft.txt", 3, "a" * 64),)

    async def save_attachment(self, identity, attachment_id):
        assert (identity, attachment_id) == (IDENTITY, "part-2")
        return SavedAttachment(
            "safe.txt", "safe.txt", "text/plain", 3, "a" * 64, "/data/safe.txt"
        )

    async def create_draft(self, account_id, **kwargs):
        assert account_id == "personal"
        assert kwargs["to"] == ("recipient@example.com",)
        if kwargs["subject"] == "reply":
            assert kwargs["reply_to_message"] == IDENTITY
        else:
            assert "reply_to_message" not in kwargs
        if kwargs["subject"] == "draft":
            assert kwargs.get("html_body") == "<p>body</p>"
        return DraftCreationResult(
            "personal", "Drafts", "42", "99", "<draft@example.com>", (), "draft-1"
        )

    async def update_draft(self, account_id, draft_id, **kwargs):
        assert (account_id, draft_id) == ("personal", "draft-1")
        assert kwargs["subject"] == "updated"
        assert kwargs.get("html_body") is None
        return DraftUpdateResult(
            "personal",
            "draft-1",
            "Drafts",
            "42",
            "100",
            "<draft@example.com>",
            (),
            "replace",
        )

    async def set_star(self, identity, starred, client_id=None):
        assert (identity, starred) == (IDENTITY, True)
        return FlagChange(IDENTITY, "starred", True, True, (), (r"\Flagged",))

    async def set_read_state(self, identity, read, client_id=None):
        assert (identity, read) == (IDENTITY, False)
        return FlagChange(IDENTITY, "read", False, True, (r"\Seen",), ())

    async def set_read_state_batch(self, identities, read, client_id=None):
        assert (identities, read) == ((IDENTITY,), False)
        change = FlagChange(IDENTITY, "read", False, True, (r"\Seen",), ())
        return (BatchFlagChange(IDENTITY, True, change),)

    async def set_star_batch(self, identities, starred, client_id=None):
        assert (identities, starred) == ((IDENTITY,), True)
        change = FlagChange(IDENTITY, "starred", True, True, (), (r"\Flagged",))
        return (BatchFlagChange(IDENTITY, True, change),)

    async def move_email(self, identity, destination_mailbox, client_id=None):
        assert (identity, destination_mailbox) == (IDENTITY, "Archive")
        destination = MessageIdentity("personal", "Archive", "77", "99")
        return MoveResult(IDENTITY, "Archive", destination)

    async def move_emails_batch(
        self, identities, destination_mailbox, client_id=None
    ):
        assert (identities, destination_mailbox) == ((IDENTITY,), "Archive")
        move = await self.move_email(IDENTITY, destination_mailbox, client_id)
        return (BatchMoveResult(IDENTITY, True, move),)


async def exercise_server() -> None:
    server = create_server(FakeBroker())
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        listed = await session.list_tools()
        assert {tool.name for tool in listed.tools} == {
            "list_accounts",
            "list_mailboxes",
            "search_emails",
            "get_email",
            "get_emails",
            "get_email_html",
            "list_attachment_inputs",
            "save_attachment",
            "create_draft",
            "update_draft",
            "set_star",
            "set_read_state",
            "set_read_state_batch",
            "set_star_batch",
            "move_email",
            "move_emails_batch",
        }
        schemas = repr([tool.inputSchema for tool in listed.tools]).casefold()
        for forbidden in ("password", "credential", "hostname", "raw_imap"):
            assert forbidden not in schemas
        tools = {tool.name: tool for tool in listed.tools}
        assert tools["list_accounts"].outputSchema["type"] == "object"
        account_schema = repr(tools["list_accounts"].outputSchema)
        for field in (
            "id", "username", "host", "port", "enabled", "sender_address",
            "sender_name",
        ):
            assert field in account_schema
        search_schema = repr(tools["search_emails"].outputSchema)
        for field in ("account_id", "mailbox", "uid_validity", "uid", "size"):
            assert field in search_schema
        assert tools["get_email"].annotations.readOnlyHint is True
        assert tools["set_star"].annotations.readOnlyHint is False
        assert tools["set_star"].annotations.idempotentHint is True
        assert not tools["set_star"].meta
        assert tools["create_draft"].annotations.idempotentHint is False
        assert not tools["create_draft"].meta
        assert tools["update_draft"].annotations.destructiveHint is True
        assert tools["move_email"].annotations.destructiveHint is True
        assert tools["move_email"].annotations.idempotentHint is False

        accounts = await session.call_tool("list_accounts")
        assert accounts.isError is False
        assert accounts.structuredContent == {
            "result": [
                {
                    "id": "personal",
                    "username": "l***@example.com",
                    "host": "imap.example.com",
                    "port": 993,
                    "enabled": True,
                    "sender_address": "leo@example.com",
                    "sender_name": "Leo Phoenix",
                }
            ]
        }

        search = await session.call_tool(
            "search_emails",
            {
                "accounts": ["personal"],
                "mailboxes": ["INBOX"],
                "limit": 1,
                "fields": ["subject"],
            },
        )
        assert search.isError is False
        assert search.structuredContent["results"][0]["identity"]["uid"] == "7"
        assert search.structuredContent["truncated"] is False
        assert search.structuredContent["order"] == "mailbox_uid_desc"
        assert search.structuredContent["results"][0]["headers"] == {
            "subject": "test"
        }
        assert search.structuredContent["targets_searched"] == [
            {"account_id": "personal", "mailbox": "INBOX"}
        ]
        assert search.structuredContent["targets_pending"] == []

        message = await session.call_tool(
            "get_email",
            {
                "account_id": "personal",
                "mailbox": "INBOX",
                "uid_validity": "42",
                "uid": "7",
            },
        )
        assert message.isError is False, message.content
        assert message.structuredContent["text"] == "plain text"

        messages = await session.call_tool(
            "get_emails",
            {
                "identities": [
                    {
                        "account_id": "personal",
                        "mailbox": "INBOX",
                        "uid_validity": "42",
                        "uid": "7",
                    }
                ]
            },
        )
        assert messages.isError is False, messages.content
        assert messages.structuredContent["result"][0]["message"]["text"] == "plain text"

        html = await session.call_tool(
            "get_email_html",
            {
                "account_id": "personal",
                "mailbox": "INBOX",
                "uid_validity": "42",
                "uid": "7",
            },
        )
        assert html.isError is False
        assert html.structuredContent["html"] == "<p>safe</p>"

        inputs = await session.call_tool("list_attachment_inputs")
        assert inputs.structuredContent["result"][0]["name"] == "draft.txt"

        attachment = await session.call_tool(
            "save_attachment",
            {
                "account_id": "personal",
                "mailbox": "INBOX",
                "uid_validity": "42",
                "uid": "7",
                "attachment_id": "part-2",
            },
        )
        assert attachment.structuredContent["saved_name"] == "safe.txt"
        assert attachment.structuredContent["sha256"] == "a" * 64
        assert attachment.structuredContent["saved_path"] == "/data/safe.txt"

        draft = await session.call_tool(
            "create_draft",
            {
                "account_id": "personal",
                "to": ["recipient@example.com"],
                "subject": "draft",
                "body": "body",
                "html_body": "<p>body</p>",
            },
        )
        assert draft.isError is False, draft.content
        assert draft.structuredContent["mailbox"] == "Drafts"
        assert draft.structuredContent["uid"] == "99"
        assert draft.structuredContent["draft_id"] == "draft-1"

        reply = await session.call_tool(
            "create_draft",
            {
                "account_id": "personal",
                "to": ["recipient@example.com"],
                "subject": "reply",
                "body": "body",
                "reply_to_message": {
                    "account_id": "personal", "mailbox": "INBOX",
                    "uid_validity": "42", "uid": "7",
                },
            },
        )
        assert reply.isError is False, reply.content

        updated = await session.call_tool(
            "update_draft",
            {
                "account_id": "personal",
                "draft_id": "draft-1",
                "to": ["recipient@example.com"],
                "subject": "updated",
                "body": "body two",
                "html_body": None,
            },
        )
        assert updated.isError is False, updated.content
        assert updated.structuredContent["uid"] == "100"
        assert updated.structuredContent["method"] == "replace"

        starred = await session.call_tool(
            "set_star",
            {
                "account_id": "personal",
                "mailbox": "INBOX",
                "uid_validity": "42",
                "uid": "7",
                "starred": True,
            },
        )
        assert starred.structuredContent["state"] == "starred"
        assert starred.structuredContent["new_flags"] == [r"\Flagged"]

        unread = await session.call_tool(
            "set_read_state",
            {
                "account_id": "personal",
                "mailbox": "INBOX",
                "uid_validity": "42",
                "uid": "7",
                "read": False,
            },
        )
        assert unread.structuredContent["state"] == "read"
        assert unread.structuredContent["enabled"] is False

        batch_unread = await session.call_tool(
            "set_read_state_batch",
            {
                "identities": [
                    {
                        "account_id": "personal",
                        "mailbox": "INBOX",
                        "uid_validity": "42",
                        "uid": "7",
                    }
                ],
                "read": False,
            },
        )
        assert batch_unread.isError is False, batch_unread.content
        assert batch_unread.structuredContent["result"][0]["ok"] is True

        batch_starred = await session.call_tool(
            "set_star_batch",
            {
                "identities": [
                    {
                        "account_id": "personal",
                        "mailbox": "INBOX",
                        "uid_validity": "42",
                        "uid": "7",
                    }
                ],
                "starred": True,
            },
        )
        assert batch_starred.isError is False, batch_starred.content
        assert batch_starred.structuredContent["result"][0]["change"]["state"] == "starred"

        moved = await session.call_tool(
            "move_email",
            {
                "account_id": "personal",
                "mailbox": "INBOX",
                "uid_validity": "42",
                "uid": "7",
                "destination_mailbox": "Archive",
            },
        )
        assert moved.isError is False, moved.content
        assert moved.structuredContent["destination_identity"]["uid"] == "99"
        assert moved.structuredContent["method"] == "uid_move"

        batch_moved = await session.call_tool(
            "move_emails_batch",
            {
                "identities": [
                    {
                        "account_id": "personal",
                        "mailbox": "INBOX",
                        "uid_validity": "42",
                        "uid": "7",
                    }
                ],
                "destination_mailbox": "Archive",
            },
        )
        assert batch_moved.isError is False, batch_moved.content
        assert batch_moved.structuredContent["result"][0]["move"][
            "destination_mailbox"
        ] == "Archive"

        rejected = await session.call_tool(
            "search_emails", {"accounts": ["personal"], "limit": 501}
        )
        assert rejected.isError is True

        combined = await session.call_tool(
            "search_emails",
            {
                "accounts": ["personal", "other"],
                "mailboxes": [f"Box-{index}" for index in range(11)],
                "limit": 501,
            },
        )
        combined_text = repr(combined.content)
        assert combined.isError is True
        assert "between 1 and 500" in combined_text
        assert "at most 20" in combined_text
        assert "over 50" in combined_text


def test_fastmcp_capability_surface_end_to_end() -> None:
    asyncio.run(exercise_server())


def test_frontend_lease_precedes_server_construction(monkeypatch) -> None:
    events: list[str] = []

    class FakePaths:
        ipc_address = "broker-endpoint"

        def ensure_private(self) -> None:
            events.append("ensure-private")

        def load_or_create_ipc_key(self) -> bytes:
            events.append("load-key")
            return b"k" * 32

    class FakeClient:
        def __init__(self, address: str, authkey: bytes) -> None:
            assert (address, authkey) == ("broker-endpoint", b"k" * 32)
            events.append("client")

        @contextmanager
        def frontend_lease(self):
            events.append("lease-enter")
            try:
                yield
            finally:
                events.append("lease-exit")

    class FakeServer:
        def run(self, *, transport: str) -> None:
            assert transport == "stdio"
            events.append("run")

    def fake_create_server(backend) -> FakeServer:
        assert isinstance(backend, FakeClient)
        events.append("create-server")
        return FakeServer()

    monkeypatch.setattr(server_module, "current_app_paths", FakePaths)
    monkeypatch.setattr(server_module, "IpcBrokerClient", FakeClient)
    monkeypatch.setattr(server_module, "create_server", fake_create_server)

    server_module.main(hold_lease=True)

    assert events == [
        "ensure-private",
        "load-key",
        "client",
        "lease-enter",
        "create-server",
        "run",
        "lease-exit",
    ]
