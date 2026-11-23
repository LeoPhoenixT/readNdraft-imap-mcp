from __future__ import annotations

import asyncio
import imaplib
from dataclasses import replace

import pytest

from readndraft_imap_mcp.audit import AuditUnavailableError
from readndraft_imap_mcp.broker import AccountConfig, AccountRegistry, BrokerService
from readndraft_imap_mcp.imap.client import (
    ImapClient,
    ImapClientError,
    ImapMovePartialError,
)
from readndraft_imap_mcp.imap.errors import ImapMoveOutcomeUnknownError
from readndraft_imap_mcp.imap.models import MessageIdentity, MoveResult

IDENTITY = MessageIdentity("personal", "INBOX", "42", "7")


class MoveConnection:
    def __init__(
        self,
        *,
        capabilities: bytes = b"IMAP4rev1 MOVE UIDPLUS",
        source_flags: tuple[str, ...] = (r"\HasNoChildren",),
        destination_flags: tuple[str, ...] = (r"\HasNoChildren",),
        uid_validity: bytes = b"42",
        copyuid: bytes | None = b"[COPYUID 77 7 99] moved",
        missing_uid: bool = False,
        move_status: str = "OK",
        copy_status: str = "OK",
        store_status: str = "OK",
        expunge_status: str = "OK",
    ) -> None:
        self.capabilities = capabilities
        self.source_flags = source_flags
        self.destination_flags = destination_flags
        self.uid_validity = uid_validity
        self.copyuid = copyuid
        self.missing_uid = missing_uid
        self.move_status = move_status
        self.copy_status = copy_status
        self.store_status = store_status
        self.expunge_status = expunge_status
        self.commands: list[tuple] = []

    @staticmethod
    def _line(name: str, flags: tuple[str, ...]) -> bytes:
        return f'({" ".join(flags)}) "/" "{name}"'.encode()

    def capability(self):
        return "OK", [self.capabilities]

    def list(self):
        return "OK", [
            self._line("INBOX", self.source_flags),
            self._line("Archive", self.destination_flags),
            self._line("Trash-custom", (r"\HasNoChildren",)),
        ]

    def select(self, mailbox, readonly=False):
        self.commands.append(("SELECT", mailbox, readonly))
        return "OK", [b"1"]

    def response(self, name):
        if name == "UIDVALIDITY":
            return name, [self.uid_validity]
        return name, None

    def uid(self, *args):
        self.commands.append(args)
        if args == ("FETCH", "7", "(UID FLAGS)"):
            if self.missing_uid:
                return "OK", [b""]
            return "OK", [(b"1 (UID 7 FLAGS (\\Seen))", b"")]
        if args[0] == "COPY":
            return self.copy_status, [self.copyuid or b"copied"]
        if args[0] == "STORE":
            return self.store_status, [b"stored"]
        if args[0] == "EXPUNGE":
            return self.expunge_status, [b"expunged"]
        raise AssertionError(args)

    def _simple_command(self, *args):
        self.commands.append(args)
        assert args[:3] == ("UID", "MOVE", "7")
        return self.move_status, [self.copyuid or b"moved"]


def build_client(connection: MoveConnection) -> ImapClient:
    client = ImapClient(
        AccountConfig("personal", "mail.example.com", 993, "leo@example.com"),
        "secret",
    )
    client.connection = connection
    return client


def test_uid_move_returns_copyuid_destination_identity() -> None:
    connection = MoveConnection()
    result = build_client(connection).move_email(IDENTITY, "Archive")

    assert result.identity == IDENTITY
    assert result.destination_identity == MessageIdentity(
        "personal", "Archive", "77", "99"
    )
    assert result.method == "uid_move"
    assert ("UID", "MOVE", "7", '"Archive"') in connection.commands
    assert all(command[0] not in {"COPY", "STORE", "EXPUNGE"} for command in connection.commands)


def test_uid_move_succeeds_when_server_omits_copyuid() -> None:
    result = build_client(MoveConnection(copyuid=None)).move_email(IDENTITY, "Archive")
    assert result.destination_identity is None


def test_message_move_requires_uidplus() -> None:
    with pytest.raises(ImapClientError, match="requires UIDPLUS"):
        build_client(MoveConnection(capabilities=b"IMAP4rev1 MOVE")).move_email(
            IDENTITY, "Archive"
        )


def test_uidplus_fallback_uses_only_targeted_broker_private_commands() -> None:
    connection = MoveConnection(capabilities=b"IMAP4rev1 UIDPLUS")
    result = build_client(connection).move_email(IDENTITY, "Archive")

    assert result.method == "uidplus_copy_delete"
    assert result.destination_identity == MessageIdentity(
        "personal", "Archive", "77", "99"
    )
    assert ("COPY", "7", '"Archive"') in connection.commands
    assert ("STORE", "7", "+FLAGS.SILENT", r"(\Deleted)") in connection.commands
    assert ("EXPUNGE", "7") in connection.commands


def test_uidplus_fallback_requires_copyuid_before_source_delete() -> None:
    connection = MoveConnection(
        capabilities=b"IMAP4rev1 UIDPLUS", copyuid=None
    )
    with pytest.raises(ImapMovePartialError, match="source retained"):
        build_client(connection).move_email(IDENTITY, "Archive")
    assert all(command[0] not in {"STORE", "EXPUNGE"} for command in connection.commands)


@pytest.mark.parametrize("failure", ["store_status", "expunge_status"])
def test_uidplus_fallback_reports_partial_copy_and_attempts_source_rollback(
    failure: str,
) -> None:
    connection = MoveConnection(
        capabilities=b"IMAP4rev1 UIDPLUS", **{failure: "NO"}
    )
    with pytest.raises(ImapMovePartialError, match="requires review"):
        build_client(connection).move_email(IDENTITY, "Archive")
    assert ("STORE", "7", "-FLAGS.SILENT", r"(\Deleted)") in connection.commands


@pytest.mark.parametrize("flag", [r"\Trash", r"\Junk", r"\Drafts", r"\Sent"])
@pytest.mark.parametrize("blocked_side", ["source", "destination"])
def test_uid_move_blocks_special_use_in_both_directions(
    flag: str, blocked_side: str
) -> None:
    kwargs = {f"{blocked_side}_flags": (flag,)}
    connection = MoveConnection(**kwargs)
    with pytest.raises(PermissionError, match="prohibited"):
        build_client(connection).move_email(IDENTITY, "Archive")
    assert all(command[0] != "UID" for command in connection.commands)


@pytest.mark.parametrize("blocked_side", ["source", "destination"])
def test_uid_move_rejects_noselect(blocked_side: str) -> None:
    kwargs = {f"{blocked_side}_flags": (r"\Noselect",)}
    with pytest.raises(ValueError, match="not selectable"):
        build_client(MoveConnection(**kwargs)).move_email(IDENTITY, "Archive")


def test_uid_move_allows_similarly_named_ordinary_mailbox() -> None:
    result = build_client(MoveConnection()).move_email(IDENTITY, "Trash-custom")
    assert result.destination_mailbox == "Trash-custom"


def test_uid_move_rejects_missing_or_same_destination() -> None:
    client = build_client(MoveConnection())
    with pytest.raises(ValueError, match="exact existing"):
        client.move_email(IDENTITY, "Missing")
    with pytest.raises(ValueError, match="must differ"):
        client.move_email(IDENTITY, "INBOX")


def test_uid_move_validates_identity_before_mutation() -> None:
    with pytest.raises(ImapClientError, match="UIDVALIDITY changed"):
        build_client(MoveConnection(uid_validity=b"99")).move_email(IDENTITY, "Archive")
    with pytest.raises(ImapClientError, match="omitted uid"):
        build_client(MoveConnection(missing_uid=True)).move_email(IDENTITY, "Archive")


def test_uid_move_propagates_imap_failure_without_fallback() -> None:
    connection = MoveConnection(move_status="NO")
    with pytest.raises(ImapClientError, match="UID MOVE"):
        build_client(connection).move_email(IDENTITY, "Archive")
    assert all(command[0] != "COPY" for command in connection.commands)


class FakeCredentialStore:
    async def load_secret(self, account_id):
        return "secret"


class FakeMoveClient:
    def __init__(self, account, secret):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def move_email(self, identity, destination_mailbox):
        if identity.uid == "8":
            raise KeyError(identity.uid)
        destination = MessageIdentity(
            identity.account_id, destination_mailbox, "77", str(90 + int(identity.uid))
        )
        return MoveResult(identity, destination_mailbox, destination)


class MemoryAudit:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)


def build_broker(audit=None, client_factory=FakeMoveClient) -> BrokerService:
    return BrokerService(
        AccountRegistry(
            [AccountConfig("personal", "mail.example.com", 993, "leo@example.com")]
        ),
        FakeCredentialStore(),
        client_factory,
        audit=audit,
    )


def test_broker_move_requires_audit_and_records_destination() -> None:
    with pytest.raises(AuditUnavailableError, match="audit sink"):
        asyncio.run(build_broker().move_email(IDENTITY, "Archive"))

    audit = MemoryAudit()
    result = asyncio.run(
        build_broker(audit).move_email(IDENTITY, "Archive", client_id="client-1")
    )
    assert result.destination_identity is not None
    event = audit.events[0]
    assert event.operation == "move_email"
    assert event.destination_mailbox == "Archive"
    assert event.destination_uid_validity == "77"
    assert event.destination_uid == "97"
    assert event.movement_method == "uid_move"
    assert event.client_id == "client-1"


def test_broker_failed_move_is_audited_without_error_details() -> None:
    audit = MemoryAudit()
    missing = MessageIdentity("personal", "INBOX", "42", "8")
    with pytest.raises(KeyError):
        asyncio.run(build_broker(audit).move_email(missing, "Archive"))
    event = audit.events[0]
    assert event.operation == "move_email"
    assert event.success is False
    assert event.destination_mailbox == "Archive"
    assert event.destination_uid is None
    assert event.error_category == "not_found"
    assert "secret" not in repr(event).casefold()


def test_broker_maps_partial_fallback_to_safe_category() -> None:
    class PartialMoveClient(FakeMoveClient):
        def move_email(self, identity, destination_mailbox):
            raise ImapMovePartialError("private partial detail")

    audit = MemoryAudit()
    with pytest.raises(ImapMovePartialError):
        asyncio.run(
            build_broker(audit, PartialMoveClient).move_email(IDENTITY, "Archive")
        )
    assert audit.events[0].error_category == "partial_move"


def test_broker_move_batch_preserves_order_and_partial_success() -> None:
    audit = MemoryAudit()
    identities = (
        IDENTITY,
        MessageIdentity("personal", "INBOX", "42", "8"),
        MessageIdentity("personal", "Other", "12", "9"),
    )
    results = asyncio.run(
        build_broker(audit).move_emails_batch(identities, "Archive")
    )
    assert [item.identity.uid for item in results] == ["7", "8", "9"]
    assert [item.ok for item in results] == [True, False, True]
    assert results[1].error is not None and results[1].error.code == "not_found"
    assert [event.success for event in audit.events] == [True, False, True]


def test_broker_move_batch_rejects_duplicates_and_multiple_accounts() -> None:
    broker = build_broker(MemoryAudit())
    with pytest.raises(ValueError, match="unique"):
        asyncio.run(broker.move_emails_batch((IDENTITY, IDENTITY), "Archive"))
    other = MessageIdentity("work", "INBOX", "42", "9")
    with pytest.raises(ValueError, match="exactly one account"):
        asyncio.run(broker.move_emails_batch((IDENTITY, other), "Archive"))


@pytest.mark.parametrize("count", [0, 51])
def test_broker_move_batch_enforces_size_limit(count: int) -> None:
    identities = tuple(
        MessageIdentity("personal", "INBOX", "42", str(index + 1))
        for index in range(count)
    )
    with pytest.raises(ValueError, match="between 1 and 50"):
        asyncio.run(
            build_broker(MemoryAudit()).move_emails_batch(identities, "Archive")
        )


class TrashConnection(MoveConnection):
    def __init__(
        self,
        *,
        trash_entries: tuple[tuple[str, tuple[str, ...]], ...] = (("Deleted Items", (r"\tRaSh",)),),
        capabilities: bytes = b"IMAP4rev1 MOVE",
        disconnect_uid: str | None = None,
    ) -> None:
        super().__init__(capabilities=capabilities)
        self.trash_entries = trash_entries
        self.disconnect_uid = disconnect_uid
        self.list_calls = 0

    def list(self):
        self.list_calls += 1
        status, entries = super().list()
        if b"SPECIAL-USE" not in self.capabilities:
            entries.extend(self._line(name, flags) for name, flags in self.trash_entries)
        return status, entries

    def _untagged_response(self, status, data, name):
        assert name == "LIST"
        return status, data

    def uid(self, *args):
        if args == ("FETCH", "8", "(UID FLAGS)"):
            return "OK", [b""]
        if args == ("FETCH", "9", "(UID FLAGS)"):
            return "OK", [b"1 (UID 9 FLAGS ())"]
        return super().uid(*args)

    def _simple_command(self, *args):
        self.commands.append(args)
        if args == ("LIST", '""', '"*"', "RETURN", "(SPECIAL-USE)"):
            self.list_calls += 1
            _, entries = super().list()
            return "OK", entries + [self._line(name, flags) for name, flags in self.trash_entries]
        assert args[:2] == ("UID", "MOVE")
        if args[2] == self.disconnect_uid:
            raise imaplib.IMAP4.abort("connection lost after UID MOVE")
        return "OK", [f"[COPYUID 77 {args[2]} 99] moved".encode()]


def build_trash_broker(connection: TrashConnection, audit=None) -> BrokerService:
    class TrashClient(ImapClient):
        def __enter__(self):
            self.connection = connection
            return self

        def __exit__(self, *args):
            self.connection = None

        def bind_request_guard(self, guard, remaining_timeout):
            self._request_guard = guard

    return build_broker(audit, TrashClient)


@pytest.mark.parametrize("capabilities", [b"IMAP4rev1 MOVE", b"IMAP4rev1 MOVE SPECIAL-USE", b"IMAP4rev1 IMAP4rev2"])
def test_trash_single_discovers_server_role_and_uses_native_move(capabilities: bytes) -> None:
    connection = TrashConnection(capabilities=capabilities)
    audit = MemoryAudit()
    result = asyncio.run(build_trash_broker(connection, audit).trash_email(IDENTITY, client_id="client-1"))

    assert result.destination_identity == MessageIdentity("personal", "Deleted Items", "77", "99")
    assert result.method == "uid_move"
    assert connection.list_calls == 1
    assert ("UID", "MOVE", "7", '"Deleted Items"') in connection.commands
    assert all(command[0] not in {"COPY", "STORE", "EXPUNGE"} for command in connection.commands)
    assert [(event.operation, event.destination_mailbox, event.client_id) for event in audit.events] == [
        ("trash_email", "Deleted Items", "client-1"),
    ]


@pytest.mark.parametrize(
    ("entries", "error", "match"),
    [
        ((), ImapClientError, "exactly one"),
        ((("One", (r"\Trash",)), ("Two", (r"\Trash",))), ImapClientError, "exactly one"),
        ((("Trash", (r"\Trash", r"\Noselect")),), ValueError, "not selectable"),
        ((("Trash", (r"\Trash", r"\NonExistent")),), ValueError, "not selectable"),
        ((("Trash", (r"\Trash", r"\Junk")),), PermissionError, "prohibited"),
    ],
)
def test_trash_rejects_unusable_destination(entries, error, match) -> None:
    connection = TrashConnection(trash_entries=entries)
    with pytest.raises(error, match=match):
        build_client(connection).prepare_trash_move()
    assert not connection.commands


def test_trash_rejects_unsupported_server_before_writing() -> None:
    connection = TrashConnection(capabilities=b"IMAP4rev1 UIDPLUS")
    with pytest.raises(ImapClientError, match="native IMAP MOVE"):
        build_client(connection).prepare_trash_move()
    assert not connection.commands


def test_trash_accepts_literal_list_names_and_untagged_copyuid() -> None:
    class LiteralTrashConnection(TrashConnection):
        def list(self):
            status, entries = super().list()
            return status, entries + [(b'(\\Trash) "/" {13}', b"Deleted Items"), b""]

        def _simple_command(self, *args):
            self.commands.append(args)
            return "OK", [b"Done"]

        def response(self, name):
            if name == "COPYUID":
                return name, [b"77 7 99"]
            return super().response(name)

    connection = LiteralTrashConnection(trash_entries=())
    name, trash = build_client(connection).prepare_trash_move()
    assert name == "Deleted Items"
    assert trash(IDENTITY).destination_identity == MessageIdentity("personal", name, "77", "99")

    connection = LiteralTrashConnection(trash_entries=(("Other Trash", (r"\Trash",)),))
    with pytest.raises(ImapClientError, match="exactly one"):
        build_client(connection).prepare_trash_move()
    assert not connection.commands


def test_trash_checks_source_account_and_uid_validity() -> None:
    connection = TrashConnection()
    _, trash = build_client(connection).prepare_trash_move()
    with pytest.raises(PermissionError, match="another account"):
        trash(replace(IDENTITY, account_id="work"))
    with pytest.raises(PermissionError, match="source mailbox"):
        trash(replace(IDENTITY, mailbox="Deleted Items"))
    with pytest.raises(ImapClientError, match="UIDVALIDITY"):
        trash(replace(IDENTITY, uid_validity="12"))
    assert connection.commands == [("SELECT", '"INBOX"', False)]


@pytest.mark.parametrize("direction", ["source", "destination"])
def test_ordinary_move_rejects_trash_reported_only_by_extended_list(direction: str) -> None:
    connection = TrashConnection(capabilities=b"IMAP4rev1 MOVE UIDPLUS SPECIAL-USE")
    identity = replace(IDENTITY, mailbox="Deleted Items") if direction == "source" else IDENTITY
    destination = "Archive" if direction == "source" else "Deleted Items"
    with pytest.raises(PermissionError, match=direction):
        build_client(connection).move_email(identity, destination)
    assert len(connection.commands) == 1


def test_trash_batch_reuses_discovery_and_reports_ordered_partial_results() -> None:
    connection = TrashConnection()
    audit = MemoryAudit()
    identities = (IDENTITY, replace(IDENTITY, mailbox="Deleted Items"), replace(IDENTITY, uid="9"))
    broker = build_trash_broker(connection, audit)
    results = asyncio.run(broker.trash_emails_batch(identities))

    assert [item.identity for item in results] == list(identities)
    assert [item.ok for item in results] == [True, False, True]
    assert results[1].error.code == "permission_denied"
    assert connection.list_calls == 1
    assert [(event.operation, event.success, event.destination_mailbox) for event in audit.events] == [
        ("trash_email", True, "Deleted Items"),
        ("trash_email", False, "Deleted Items"),
        ("trash_email", True, "Deleted Items"),
    ]


def test_trash_batch_rejects_duplicates_cross_account_and_missing_trash_before_writing() -> None:
    connection = TrashConnection(trash_entries=())
    audit = MemoryAudit()
    broker = build_trash_broker(connection, audit)
    with pytest.raises(ValueError, match="unique"):
        asyncio.run(broker.trash_emails_batch((IDENTITY, IDENTITY)))
    with pytest.raises(ValueError, match="exactly one account"):
        asyncio.run(broker.trash_emails_batch((IDENTITY, replace(IDENTITY, account_id="work"))))
    assert connection.list_calls == 0

    results = asyncio.run(broker.trash_emails_batch((IDENTITY, replace(IDENTITY, uid="9"))))
    assert [item.error.code for item in results] == ["imap_error", "imap_error"]
    assert [event.destination_mailbox for event in audit.events] == [None, None]
    assert connection.list_calls == 1
    assert not connection.commands


def test_trash_batch_stops_after_unknown_move_and_audits_remaining_items() -> None:
    connection = TrashConnection(disconnect_uid="7")
    audit = MemoryAudit()
    broker = build_trash_broker(connection, audit)
    results = asyncio.run(broker.trash_emails_batch((IDENTITY, replace(IDENTITY, uid="9"))))

    assert [item.error.code for item in results] == ["outcome_unknown", "connection_error"]
    assert [event.error_category for event in audit.events] == ["outcome_unknown", "connection_error"]
    assert [command[2] for command in connection.commands if command[:2] == ("UID", "MOVE")] == ["7"]


def test_trash_single_records_unknown_move() -> None:
    connection = TrashConnection(disconnect_uid="7")
    audit = MemoryAudit()
    with pytest.raises(ImapMoveOutcomeUnknownError):
        asyncio.run(build_trash_broker(connection, audit).trash_email(IDENTITY))
    assert audit.events[0].error_category == "outcome_unknown"
    assert audit.events[0].destination_mailbox == "Deleted Items"
