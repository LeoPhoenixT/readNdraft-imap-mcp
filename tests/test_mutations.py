from __future__ import annotations

import asyncio
import json

import pytest

from readndraft_imap_mcp.audit import AuditEvent, AuditUnavailableError, JsonlAuditSink
from readndraft_imap_mcp.broker import AccountConfig, AccountRegistry, BrokerService
from readndraft_imap_mcp.imap.client import ImapClient, ImapClientError
from readndraft_imap_mcp.imap.models import FlagChange, MessageIdentity

IDENTITY = MessageIdentity("personal", "INBOX", "42", "7")


class MutationConnection:
    def __init__(self, flags=(r"\Seen", "$Custom"), uid_validity=b"42") -> None:
        self.flags = set(flags)
        self.uid_validity = uid_validity
        self.commands = []

    def select(self, mailbox, readonly=False):
        assert mailbox == '"INBOX"'
        assert readonly is False
        return "OK", [b"1"]

    def response(self, name):
        return name, [self.uid_validity]

    def uid(self, *args):
        self.commands.append(args)
        if args[0] == "FETCH":
            flags = " ".join(sorted(self.flags)).encode()
            return "OK", [(b"1 (UID 7 FLAGS (" + flags + b"))", b"")]
        assert args[0] == "STORE"
        flag = args[3][1:-1]
        if args[2] == "+FLAGS.SILENT":
            self.flags.add(flag)
        else:
            self.flags.discard(flag)
        return "OK", [b"stored"]


def build_client(connection: MutationConnection) -> ImapClient:
    client = ImapClient(
        AccountConfig("personal", "mail.example.com", 993, "leo@example.com"),
        "secret",
    )
    client.connection = connection
    return client


def test_star_changes_only_flagged_and_preserves_unrelated_flags() -> None:
    connection = MutationConnection()
    result = build_client(connection).set_star(IDENTITY, True)

    assert result.changed is True
    assert set(result.new_flags) == {r"\Seen", r"\Flagged", "$Custom"}
    assert ("STORE", "7", "+FLAGS.SILENT", r"(\Flagged)") in connection.commands


def test_read_state_changes_only_seen() -> None:
    connection = MutationConnection()
    result = build_client(connection).set_read_state(IDENTITY, False)

    assert result.changed is True
    assert set(result.new_flags) == {"$Custom"}
    assert ("STORE", "7", "-FLAGS.SILENT", r"(\Seen)") in connection.commands


def test_uidvalidity_mismatch_prevents_store() -> None:
    connection = MutationConnection(uid_validity=b"99")
    with pytest.raises(ImapClientError, match="UIDVALIDITY changed"):
        build_client(connection).set_star(IDENTITY, True)
    assert all(command[0] != "STORE" for command in connection.commands)


class FakeCredentialStore:
    async def load_secret(self, account_id):
        return "secret"


class FakeMutationClient:
    def __init__(self, account, secret):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def set_star(self, identity, starred):
        return FlagChange(identity, "starred", starred, True, (), (r"\Flagged",))

    def set_read_state(self, identity, read):
        if identity.uid == "8":
            raise KeyError(identity.uid)
        old_flags = () if read else (r"\Seen",)
        new_flags = (r"\Seen",) if read else ()
        return FlagChange(identity, "read", read, True, old_flags, new_flags)


class FailingMutationClient(FakeMutationClient):
    def set_star(self, identity, starred):
        raise RuntimeError("simulated failure")


class MemoryAudit:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)


def build_broker(audit=None, client_factory=FakeMutationClient) -> BrokerService:
    return BrokerService(
        AccountRegistry(
            [AccountConfig("personal", "mail.example.com", 993, "leo@example.com")]
        ),
        FakeCredentialStore(),
        client_factory,
        audit=audit,
    )


def test_broker_refuses_mutation_without_audit_sink() -> None:
    with pytest.raises(AuditUnavailableError, match="audit sink"):
        asyncio.run(build_broker().set_star(IDENTITY, True))


def test_successful_mutation_records_semantic_audit_event() -> None:
    audit = MemoryAudit()
    result = asyncio.run(build_broker(audit).set_star(IDENTITY, True, "client-1"))

    assert result.enabled is True
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.operation == "set_star"
    assert event.success is True
    assert event.old_state is False
    assert event.new_state is True
    assert event.client_id == "client-1"
    assert "secret" not in repr(event).casefold()


def test_jsonl_audit_sink_persists_only_safe_event_fields(tmp_path) -> None:
    path = (tmp_path / "audit.jsonl").resolve()
    sink = JsonlAuditSink(path)
    event = AuditEvent.mutation(
        operation="set_read_state",
        account_id="personal",
        mailbox="INBOX",
        uid="7",
        success=True,
        duration_ms=2,
        old_state=False,
        new_state=True,
    )

    asyncio.run(sink.record(event))

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["operation"] == "set_read_state"
    assert stored["old_state"] is False
    assert stored["new_state"] is True
    assert "secret" not in repr(stored).casefold()
    assert stored["previous_hash"] == "0" * 64
    assert len(stored["entry_hash"]) == 64
    assert asyncio.run(sink.verify()) == 1


def test_jsonl_audit_sink_detects_tampering(tmp_path) -> None:
    path = (tmp_path / "audit.jsonl").resolve()
    sink = JsonlAuditSink(path)
    event = AuditEvent.mutation(
        operation="set_star",
        account_id="personal",
        mailbox="INBOX",
        uid="7",
        success=True,
        duration_ms=1,
    )
    asyncio.run(sink.record(event))
    asyncio.run(sink.record(event))
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace('"success":true', '"success":false')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="integrity"):
        asyncio.run(JsonlAuditSink(path).verify())


def test_failed_mutation_is_audited_without_error_details() -> None:
    audit = MemoryAudit()
    with pytest.raises(RuntimeError, match="simulated failure"):
        asyncio.run(
            build_broker(audit, FailingMutationClient).set_star(IDENTITY, True)
        )

    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.success is False
    assert event.error_category == "RuntimeError"
    assert event.old_state is None
    assert "simulated failure" not in repr(event)


def test_batch_read_state_preserves_order_audits_and_isolates_failure() -> None:
    audit = MemoryAudit()
    identities = (
        IDENTITY,
        MessageIdentity("personal", "INBOX", "42", "8"),
        MessageIdentity("personal", "INBOX", "42", "9"),
    )

    results = asyncio.run(
        build_broker(audit).set_read_state_batch(identities, False, client_id="client-1")
    )

    assert [item.identity.uid for item in results] == ["7", "8", "9"]
    assert [item.ok for item in results] == [True, False, True]
    assert results[1].error == "not_found"
    assert len(audit.events) == 3
    assert [event.success for event in audit.events] == [True, False, True]
    assert all(event.operation == "set_read_state" for event in audit.events)


def test_batch_rejects_duplicate_identity_before_mutation() -> None:
    with pytest.raises(ValueError, match="unique"):
        asyncio.run(
            build_broker(MemoryAudit()).set_read_state_batch(
                (IDENTITY, IDENTITY), False
            )
        )


def test_batch_star_uses_semantic_star_operation() -> None:
    audit = MemoryAudit()
    results = asyncio.run(build_broker(audit).set_star_batch((IDENTITY,), True))

    assert results[0].ok is True
    assert results[0].change is not None
    assert results[0].change.state == "starred"
    assert audit.events[0].operation == "set_star"
