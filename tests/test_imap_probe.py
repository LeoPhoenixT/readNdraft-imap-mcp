from __future__ import annotations

import pytest

from readndraft_imap_mcp.poc.imap_probe import (
    ImapProbe,
    Mailbox,
    ProbeError,
    parse_mailbox_list,
)


def test_parse_mailboxes_and_special_use() -> None:
    result = parse_mailbox_list(
        [
            br'(\HasNoChildren) "/" "INBOX"',
            br'(\HasNoChildren \Drafts) "/" "Drafts"',
        ]
    )
    assert result == [
        Mailbox("INBOX", "/", (r"\HasNoChildren",)),
        Mailbox("Drafts", "/", (r"\HasNoChildren", r"\Drafts")),
    ]
    assert result[1].is_drafts is True


def test_parse_nil_delimiter_and_unquoted_mailbox() -> None:
    assert parse_mailbox_list([br"(\Noselect) NIL Archive"]) == [
        Mailbox("Archive", None, (r"\Noselect",))
    ]


def test_plain_auth_uses_sasl_plain(monkeypatch) -> None:
    observed = {}

    class FakeConnection:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def authenticate(self, mechanism, callback):
            observed["mechanism"] = mechanism
            observed["payload"] = callback(b"")
            return "OK", [b"authenticated"]

        def logout(self):
            return "BYE", [b"logout"]

    monkeypatch.setattr("imaplib.IMAP4_SSL", FakeConnection)

    with ImapProbe(
        "imap.example.com",
        993,
        "user@example.com",
        "temporary-secret",
        auth_method="plain",
    ) as probe:
        assert probe._secret == ""

    assert observed == {
        "mechanism": "PLAIN",
        "payload": b"\0user@example.com\0temporary-secret",
    }


class SearchConnection:
    def __init__(self, search_result: bytes) -> None:
        self.search_result = search_result
        self.search_args = None

    def select(self, mailbox, readonly=False):
        assert mailbox == "INBOX"
        assert readonly is True
        return "OK", [b"1"]

    def response(self, name):
        assert name == "UIDVALIDITY"
        return name, [b"42"]

    def uid(self, *args):
        self.search_args = args
        return "OK", [self.search_result]


def test_find_unique_uid_by_subject() -> None:
    connection = SearchConnection(b"731")
    probe = ImapProbe("imap.example.com", 993, "user@example.com", "secret")
    probe.connection = connection

    assert probe.find_unique_uid_by_subject("INBOX", 'readNdraft "peek"') == "731"
    assert connection.search_args == (
        "SEARCH",
        None,
        "HEADER",
        "SUBJECT",
        '"readNdraft \\"peek\\""',
    )


@pytest.mark.parametrize(
    ("search_result", "message"),
    [
        (b"", "No message matched"),
        (b"731 732", "matched 2 messages"),
    ],
)
def test_subject_lookup_requires_exactly_one_match(
    search_result: bytes, message: str
) -> None:
    probe = ImapProbe("imap.example.com", 993, "user@example.com", "secret")
    probe.connection = SearchConnection(search_result)

    with pytest.raises(ProbeError, match=message):
        probe.find_unique_uid_by_subject("INBOX", "unique test subject")


