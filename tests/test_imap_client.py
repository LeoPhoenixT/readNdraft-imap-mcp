from __future__ import annotations

from email.message import EmailMessage

import pytest

from readndraft_imap_mcp.broker.accounts import AccountConfig
from readndraft_imap_mcp.drafts import DraftProvenance
from readndraft_imap_mcp.imap.client import ImapClient, ImapClientError
from readndraft_imap_mcp.imap.models import MessageIdentity, SearchFilters


class ReadConnection:
    def __init__(self, raw: bytes, *, trailing_flags: bool) -> None:
        self.raw = raw
        self.trailing_flags = trailing_flags
        self.commands = []

    def select(self, mailbox, readonly=False):
        assert mailbox == '"INBOX"'
        assert readonly is True
        return "OK", [b"1"]

    def response(self, name):
        return name, [b"42"]

    def uid(self, *args):
        self.commands.append(args)
        if args[2] == "(UID FLAGS RFC822.SIZE)":
            return "OK", [(b"1 (UID 7 FLAGS () RFC822.SIZE 120)", b"")]
        assert args[2] == "(BODY.PEEK[] FLAGS)"
        if self.trailing_flags:
            return "OK", [(b"1 (UID 7 BODY[] {120}", self.raw), b" FLAGS ())"]
        return "OK", [(b"1 (UID 7 FLAGS () BODY[] {120}", self.raw), b")"]


@pytest.mark.parametrize("trailing_flags", [False, True])
def test_message_read_uses_body_peek_and_preserves_flags(
    trailing_flags: bool,
) -> None:
    message = EmailMessage()
    message["Subject"] = "Unread test"
    message.set_content(r"body with attacker-controlled FLAGS (\Seen)")
    connection = ReadConnection(message.as_bytes(), trailing_flags=trailing_flags)
    client = ImapClient(
        AccountConfig("personal", "mail.example.com", 993, "leo@example.com"),
        "secret",
    )
    client.connection = connection
    identity = MessageIdentity("personal", "INBOX", "42", "7")

    result = client.get_message(identity)

    assert result.text.strip() == r"body with attacker-controlled FLAGS (\Seen)"
    assert result.flags == ()
    assert connection.commands[-1][2] == "(BODY.PEEK[] FLAGS)"


class MailboxListConnection:
    def list(self):
        return "OK", [
            br'(\HasNoChildren) "/" "INBOX/Jira&-Confluence"',
            br'(\HasNoChildren) "/" "&ZeVnLIqe-"',
        ]


def test_mailbox_list_preserves_raw_name_and_decodes_display_name() -> None:
    client = ImapClient(
        AccountConfig("personal", "mail.example.com", 993, "leo@example.com"),
        "secret",
    )
    client.connection = MailboxListConnection()

    mailboxes = client.list_mailboxes()

    assert mailboxes[0].name == "INBOX/Jira&-Confluence"
    assert mailboxes[0].display_name == "INBOX/Jira&Confluence"
    assert mailboxes[1].name == "&ZeVnLIqe-"
    assert mailboxes[1].display_name == "日本語"


class SearchConnection:
    def __init__(self) -> None:
        self.search_args = None
        self.selected = None

    def select(self, mailbox, readonly=False):
        self.selected = mailbox
        assert readonly is True
        return "OK", [b"0"]

    def response(self, name):
        return name, [b"42"]

    def uid(self, *args):
        self.search_args = args
        return "OK", [b""]


def test_attachment_filename_search_is_bounded_and_semantic() -> None:
    connection = SearchConnection()
    client = ImapClient(
        AccountConfig("personal", "mail.example.com", 993, "leo@example.com"),
        "secret",
    )
    client.connection = connection

    assert client.search(
        "INBOX", SearchFilters(attachment_filename="report.pdf"), limit=10
    ) == ()
    assert connection.search_args == (
        "SEARCH",
        None,
        "HEADER",
        "Content-Disposition",
        '"report.pdf"',
    )
    assert connection.selected == '"INBOX"'


def test_search_quotes_raw_mailbox_names_with_spaces() -> None:
    connection = SearchConnection()
    client = ImapClient(
        AccountConfig("personal", "mail.example.com", 993, "leo@example.com"),
        "secret",
    )
    client.connection = connection

    assert client.search("Infected Items", SearchFilters(), limit=10) == ()
    assert connection.selected == '"Infected Items"'


class SearchSummaryConnection:
    def __init__(
        self,
        *,
        summary_flags: bool,
        standalone: bytes | None = None,
        trailing_summary_metadata: bool = False,
    ) -> None:
        self.summary_flags = summary_flags
        self.standalone = standalone or b"1 (UID 7 FLAGS (\\Seen $Label1))"
        self.trailing_summary_metadata = trailing_summary_metadata
        self.commands = []

    def select(self, mailbox, readonly=False):
        self.commands.append(("SELECT", mailbox, readonly))
        assert (mailbox, readonly) == ('"INBOX"', True)
        return "OK", [b"1"]

    def response(self, name):
        return name, [b"42"]

    def uid(self, *args):
        self.commands.append(args)
        if args[0] == "SEARCH":
            return "OK", [b"7"]
        if args[2] == "(UID FLAGS)":
            return "OK", [(self.standalone, b"")]
        assert args[0:2] == ("FETCH", "7")
        assert "BODY.PEEK[HEADER.FIELDS" in args[2]
        assert "BODY.PEEK[]" not in args[2]
        if self.trailing_summary_metadata:
            return "OK", [
                (
                    b"1 (BODY[HEADER.FIELDS] {33}",
                    b"Subject: Compatibility test\r\n\r\n",
                ),
                b' UID 7 RFC822.SIZE 120 INTERNALDATE "22-Jul-2026 11:30:00 +0800")',
            ]
        flags = b" FLAGS (\\Flagged)" if self.summary_flags else b""
        return "OK", [
            (
                b"1 (UID 7" + flags
                + b' RFC822.SIZE 120 INTERNALDATE "22-Jul-2026 11:30:00 +0800" '
                + b"BODY[HEADER.FIELDS] {33}",
                b"Subject: Compatibility test\r\n\r\n",
            ),
            b")",
        ]


@pytest.mark.parametrize("summary_flags", [True, False])
@pytest.mark.parametrize("trailing_summary_metadata", [True, False])
def test_search_fetches_accurate_flags_separately(
    summary_flags: bool,
    trailing_summary_metadata: bool,
) -> None:
    connection = SearchSummaryConnection(
        summary_flags=summary_flags,
        trailing_summary_metadata=trailing_summary_metadata,
    )
    client = ImapClient(
        AccountConfig("personal", "mail.example.com", 993, "leo@example.com"),
        "secret",
    )
    client.connection = connection

    result = client.search("INBOX", SearchFilters(read=False), limit=1)

    assert len(result) == 1
    assert result[0].identity == MessageIdentity("personal", "INBOX", "42", "7")
    assert result[0].headers["subject"] == "Compatibility test"
    assert result[0].flags == (r"\Seen", "$Label1")
    assert result[0].size == 120
    assert result[0].received_at == "2026-07-22T03:30:00Z"
    assert ("SEARCH", None, "UNSEEN") in connection.commands
    assert ("FETCH", "7", "(UID FLAGS)") in connection.commands
    summary = next(
        command for command in connection.commands
        if command[0:2] == ("FETCH", "7") and command[2] != "(UID FLAGS)"
    )
    assert " FLAGS " not in summary[2]
    assert "BODY.PEEK[HEADER.FIELDS" in summary[2]
    assert all(command[0] != "STORE" for command in connection.commands)


@pytest.mark.parametrize(
    ("standalone", "message"),
    (
        (b"1 (UID 7 RFC822.SIZE 120)", "omitted FLAGS"),
        (b"1 (UID 8 FLAGS (\\Seen))", "unexpected UID"),
    ),
)
def test_search_fails_closed_for_invalid_standalone_flags(
    standalone: bytes, message: str
) -> None:
    connection = SearchSummaryConnection(
        summary_flags=False, standalone=standalone
    )
    client = ImapClient(
        AccountConfig("personal", "mail.example.com", 993, "leo@example.com"),
        "secret",
    )
    client.connection = connection

    with pytest.raises(ImapClientError, match=message):
        client.search("INBOX", SearchFilters(), limit=1)


class PagedSearchConnection:
    def select(self, mailbox, readonly=False):
        return "OK", [b"5"]

    def response(self, name):
        return name, [b"42"]

    def uid(self, *args):
        if args[0] == "SEARCH":
            return "OK", [b"1 2 3 4 5"]
        uid = args[1]
        if args[2] == "(UID FLAGS)":
            return "OK", [(f"1 (UID {uid} FLAGS ())".encode(), b"")]
        assert "INTERNALDATE" in args[2]
        return "OK", [
            (
                (
                    f'1 (UID {uid} RFC822.SIZE 20 INTERNALDATE '
                    '"22-Jul-2026 11:30:00 +0800" BODY[HEADER.FIELDS] {15}'
                ).encode(),
                f"Subject: {uid}\r\n\r\n".encode(),
            ),
            b")",
        ]


def test_search_window_pages_by_uid_without_boundary_duplicates() -> None:
    client = ImapClient(
        AccountConfig("personal", "mail.example.com", 993, "leo@example.com"),
        "secret",
    )
    client.connection = PagedSearchConnection()

    first = client.search_window("INBOX", SearchFilters(), limit=2)
    second = client.search_window(
        "INBOX",
        SearchFilters(),
        limit=2,
        before_uid=first.next_uid,
        expected_uid_validity=first.uid_validity,
    )

    assert [item.identity.uid for item in first.results] == ["5", "4"]
    assert first.has_more is True
    assert first.next_uid == "4"
    assert [item.identity.uid for item in second.results] == ["3", "2"]
    assert set(item.identity.uid for item in first.results).isdisjoint(
        item.identity.uid for item in second.results
    )


class AppendConnection:
    def __init__(self, flags=rb"\Drafts") -> None:
        self.flags = flags
        self.appended = None

    def list(self):
        return "OK", [(b"(" + self.flags + b') "/" "Drafts"')]

    def append(self, mailbox, flags, internal_date, message):
        self.appended = (mailbox, flags, internal_date, message)
        return "OK", [b"[APPENDUID 42 99] completed"]


def test_draft_append_uses_special_use_mailbox_and_returns_appenduid() -> None:
    connection = AppendConnection()
    client = ImapClient(
        AccountConfig("personal", "mail.example.com", 993, "leo@example.com"),
        "secret",
    )
    client.connection = connection

    result = client.append_draft(b"Subject: test\r\n\r\nbody", "<id@example.com>", ())

    assert connection.appended[:3] == ('"Drafts"', r"(\Draft)", None)
    assert result.mailbox == "Drafts"
    assert result.uid_validity == "42"
    assert result.uid == "99"


def test_draft_append_fails_closed_without_special_use_mailbox() -> None:
    connection = AppendConnection(flags=rb"\HasNoChildren")
    client = ImapClient(
        AccountConfig("personal", "mail.example.com", 993, "leo@example.com"),
        "secret",
    )
    client.connection = connection
    with pytest.raises(ImapClientError, match="SPECIAL-USE"):
        client.append_draft(b"Subject: test\r\n\r\nbody", "<id@example.com>", ())


def provenance() -> DraftProvenance:
    return DraftProvenance(
        draft_id="a" * 32,
        account_id="personal",
        mailbox="Drafts",
        uid_validity="42",
        uid="99",
        message_id="<id@example.com>",
        attachment_hashes=(),
        created_at="2026-08-11T00:00:00+00:00",
        updated_at="2026-08-11T00:00:00+00:00",
    )


class UpdateConnection:
    def __init__(self, capabilities: bytes, *, trailing_flags: bool = False) -> None:
        self.capability_line = capabilities
        self.trailing_flags = trailing_flags
        self.commands = []
        self.literal = None

    def capability(self):
        return "OK", [self.capability_line]

    def list(self):
        return "OK", [br'(\Drafts) "/" "Drafts"']

    def select(self, mailbox, readonly=False):
        assert (mailbox, readonly) == ('"Drafts"', False)
        return "OK", [b"1"]

    def response(self, name):
        if name == "UIDVALIDITY":
            return name, [b"42"]
        return name, None

    def uid(self, *args):
        self.commands.append(args)
        if args[0] == "FETCH":
            if self.trailing_flags:
                return "OK", [
                    (
                        b"1 (UID 99 BODY[HEADER.FIELDS (MESSAGE-ID)] {33}",
                        b"Message-ID: <id@example.com>\r\n\r\n",
                    ),
                    b" FLAGS (\\Draft))",
                ]
            return "OK", [
                (
                    b"1 (UID 99 FLAGS (\\Draft) BODY[HEADER.FIELDS (MESSAGE-ID)] {33}",
                    b"Message-ID: <id@example.com>\r\n\r\n",
                ),
                b")",
            ]
        return "OK", [b"completed"]

    def _simple_command(self, *args):
        self.commands.append(args)
        assert self.literal == b"Subject: updated\r\n\r\nbody"
        return "OK", [b"[APPENDUID 42 100] replaced"]


@pytest.mark.parametrize("trailing_flags", [False, True])
def test_draft_update_prefers_uid_replace_and_verifies_provenance(
    trailing_flags: bool,
) -> None:
    connection = UpdateConnection(
        b"IMAP4rev1 REPLACE UIDPLUS",
        trailing_flags=trailing_flags,
    )
    client = ImapClient(
        AccountConfig("personal", "mail.example.com", 993, "leo@example.com"),
        "secret",
    )
    client.connection = connection
    result = client.replace_draft(
        provenance(),
        b"Subject: updated\r\n\r\nbody",
        "<id@example.com>",
        (),
    )
    assert result.method == "replace"
    assert result.uid == "100"
    assert connection.commands[-1][:3] == ("UID", "REPLACE", "99")


class UidplusConnection(UpdateConnection):
    def __init__(self) -> None:
        super().__init__(b"IMAP4rev1 UIDPLUS")
        self.appended = None

    def append(self, mailbox, flags, internal_date, message):
        self.appended = (mailbox, flags, internal_date, message)
        return "OK", [b"[APPENDUID 42 100] appended"]


def test_draft_update_uidplus_fallback_expunge_is_exact() -> None:
    connection = UidplusConnection()
    client = ImapClient(
        AccountConfig("personal", "mail.example.com", 993, "leo@example.com"),
        "secret",
    )
    client.connection = connection
    result = client.replace_draft(
        provenance(),
        b"Subject: updated\r\n\r\nbody",
        "<id@example.com>",
        (),
    )
    assert result.method == "uidplus"
    assert ("STORE", "99", "+FLAGS.SILENT", r"(\Deleted)") in connection.commands
    assert ("EXPUNGE", "99") in connection.commands
    assert not any(command[0] == "EXPUNGE" and len(command) == 1 for command in connection.commands)


def test_draft_update_fails_without_safe_extension() -> None:
    connection = UpdateConnection(b"IMAP4rev1")
    client = ImapClient(
        AccountConfig("personal", "mail.example.com", 993, "leo@example.com"),
        "secret",
    )
    client.connection = connection
    with pytest.raises(ImapClientError, match="REPLACE or UIDPLUS"):
        client.replace_draft(
            provenance(),
            b"Subject: updated\r\n\r\nbody",
            "<id@example.com>",
            (),
        )
