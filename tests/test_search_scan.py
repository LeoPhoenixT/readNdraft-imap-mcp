from __future__ import annotations

import pytest

from readndraft_imap_mcp.broker.accounts import AccountConfig
from readndraft_imap_mcp.imap.client import ImapClient, ImapClientError
from readndraft_imap_mcp.imap.models import SearchFilters


class MimeSearchConnection:
    def __init__(self, uids: tuple[int, ...], structures: dict[int, bytes], *, uid_next: int | None = None) -> None:
        self.uids = uids
        self.structures = structures
        self.uid_next = uid_next or max(uids, default=0) + 1
        self.commands: list[tuple[object, ...]] = []

    def select(self, mailbox, readonly=False):
        assert readonly is True
        return "OK", [b"0"]

    def response(self, name):
        return name, [b"42" if name == "UIDVALIDITY" else str(self.uid_next).encode()]

    def uid(self, *args):
        self.commands.append(args)
        if args[0] == "SEARCH":
            lower, upper = (int(value) for value in args[-1].split(":"))
            return "OK", [b" ".join(str(uid).encode() for uid in self.uids if lower <= uid <= upper)]
        uids = tuple(int(uid) for uid in args[1].split(","))
        if args[2] == "(UID FLAGS)":
            return "OK", [(f"1 (UID {uid} FLAGS ())".encode(), b"") for uid in uids]
        if "BODYSTRUCTURE" in args[2]:
            return "OK", [
                (
                    f"1 (UID {uid} RFC822.SIZE 1 INTERNALDATE "
                    f"\"22-Jul-2026 11:30:00 +0800\" BODYSTRUCTURE ".encode()
                    + self.structures[uid]
                    + b")",
                    b"",
                )
                for uid in uids
            ]
        return "OK", [
            (
                f"1 (UID {uid} RFC822.SIZE 1 INTERNALDATE "
                f"\"22-Jul-2026 11:30:00 +0800\" BODY[HEADER.FIELDS] {{16}}".encode(),
                b"Subject: match\r\n\r\n",
            )
            for uid in uids
        ]


def _client(connection: MimeSearchConnection) -> ImapClient:
    client = ImapClient(AccountConfig("personal", "mail.example.com", 993, "leo@example.com"), "secret")
    client.connection = connection
    return client


def test_attachment_search_matches_nested_rfc2231_filename_with_nfkc_casefold() -> None:
    structure = (
        b'(("TEXT" "PLAIN" NIL NIL NIL "7BIT" 1 1 NIL NIL NIL NIL) '
        b'("APPLICATION" "PDF" ("NAME*" "utf-8\'\'R%EF%BC%A5port.pdf") NIL NIL "BASE64" 1 NIL '
        b'("ATTACHMENT" NIL) NIL NIL) "MIXED")'
    )
    connection = MimeSearchConnection((2,), {2: structure})

    result = _client(connection).search("INBOX", SearchFilters(attachment_filename="report"), 10)

    assert [item.identity.uid for item in result] == ["2"]
    assert all("Content-Disposition" not in command for command in connection.commands if command[0] == "SEARCH")


def test_attachment_search_does_not_trust_top_level_content_disposition() -> None:
    connection = MimeSearchConnection((1,), {1: b'("TEXT" "PLAIN" NIL NIL NIL "7BIT" 1 1 NIL NIL NIL NIL)'})

    assert _client(connection).search("INBOX", SearchFilters(attachment_filename="report.pdf"), 10) == ()
    assert all("Content-Disposition" not in command for command in connection.commands if command[0] == "SEARCH")


def test_attachment_search_matches_outer_forwarded_message_filename_without_descending() -> None:
    structure = (
        b'("MESSAGE" "RFC822" ("NAME" "forwarded.eml") NIL NIL "7BIT" 123 '
        b'(NIL NIL NIL NIL NIL NIL NIL NIL NIL NIL) '
        b'("APPLICATION" "PDF" ("NAME" "inner.pdf") NIL NIL "BASE64" 1 NIL '
        b'("ATTACHMENT" ("FILENAME" "inner.pdf")) NIL NIL) 1 NIL '
        b'("ATTACHMENT" ("FILENAME" "forwarded.eml")) NIL NIL)'
    )
    connection = MimeSearchConnection((1,), {1: structure})

    assert [item.identity.uid for item in _client(connection).search(
        "INBOX", SearchFilters(attachment_filename="forwarded.eml"), 10
    )] == ["1"]
    assert _client(connection).search("INBOX", SearchFilters(attachment_filename="inner.pdf"), 10) == ()


def test_attachment_candidates_use_batched_bodystructure_fetches() -> None:
    structure = b'("TEXT" "PLAIN" NIL NIL NIL "7BIT" 1 1 NIL NIL NIL NIL)'
    connection = MimeSearchConnection(tuple(range(1, 31)), {uid: structure for uid in range(1, 31)})

    assert _client(connection).search("INBOX", SearchFilters(attachment_filename="none"), 10) == ()
    fetches = [command for command in connection.commands if command[0] == "FETCH" and "BODYSTRUCTURE" in command[2]]
    assert [len(command[1].split(",")) for command in fetches] == [25, 5]


def test_attachment_candidate_budget_has_a_resumable_zero_match_frontier() -> None:
    structure = b'("TEXT" "PLAIN" NIL NIL NIL "7BIT" 1 1 NIL NIL NIL NIL)'
    connection = MimeSearchConnection(tuple(range(1, 601)), {uid: structure for uid in range(1, 601)}, uid_next=601)
    client = _client(connection)

    first = client.search_window("INBOX", SearchFilters(attachment_filename="none"), 10)
    second = client.search_window("INBOX", SearchFilters(attachment_filename="none"), 10, before_uid=first.next_uid)

    assert first.results == () and first.complete is False and first.next_uid == "101"
    assert second.results == () and second.complete is True
    inspected = [
        int(uid)
        for command in connection.commands
        if command[0] == "FETCH" and "BODYSTRUCTURE" in command[2]
        for uid in command[1].split(",")
    ]
    assert set(inspected) == set(range(1, 601))
    assert len(inspected) == len(set(inspected))


def test_limit_plus_one_match_is_returned_on_the_next_page_without_a_gap() -> None:
    connection = MimeSearchConnection(tuple(range(1, 6)), {}, uid_next=6)
    client = _client(connection)

    first = client.search_window("INBOX", SearchFilters(), 2)
    second = client.search_window("INBOX", SearchFilters(), 2, before_uid=first.next_uid)

    assert [item.identity.uid for item in first.results] == ["5", "4"]
    assert [item.identity.uid for item in second.results] == ["3", "2"]
    assert set(item.identity.uid for item in first.results).isdisjoint(item.identity.uid for item in second.results)


def test_empty_uid_ranges_stop_after_twenty_server_searches() -> None:
    connection = MimeSearchConnection((), {}, uid_next=210_001)

    window = _client(connection).search_window("INBOX", SearchFilters(), 10)

    assert window.complete is False and window.next_uid == "10001"
    assert len([command for command in connection.commands if command[0] == "SEARCH"]) == 20


def test_malformed_attachment_bodystructure_fails_closed() -> None:
    connection = MimeSearchConnection((1,), {1: b"(broken"})

    with pytest.raises(ImapClientError, match="BODYSTRUCTURE"):
        _client(connection).search("INBOX", SearchFilters(attachment_filename="report"), 10)


def test_oversize_attachment_bodystructure_fails_closed() -> None:
    connection = MimeSearchConnection((1,), {1: b"(" + b"x" * (2 * 1024 * 1024) + b")"})

    with pytest.raises(ImapClientError, match="BODYSTRUCTURE"):
        _client(connection).search("INBOX", SearchFilters(attachment_filename="report"), 10)
