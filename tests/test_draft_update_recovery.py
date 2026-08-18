from __future__ import annotations

import asyncio

import pytest

from readndraft_imap_mcp.broker import AccountConfig, AccountRegistry, BrokerService
from readndraft_imap_mcp.drafts import DraftProvenanceError, FileDraftStore
from readndraft_imap_mcp.imap.models import DraftUpdateResult


class Credentials:
    async def load_secret(self, account_id):
        return "secret"


class Audit:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)


class CrashSafeClient:
    messages = {"99": "<draft@example.com>"}
    next_uid = 100
    fail_after_append = False
    fail_expunge = False
    store = None
    draft_id = None

    def __init__(self, account, secret):
        self.account = account

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def resolve_draft_uid(self, record):
        if self.messages.get(record.uid) == record.message_id:
            return (record.uid,)
        return tuple(uid for uid, message_id in self.messages.items() if message_id == record.message_id)

    def append_draft_update(self, record, raw, message_id, attachment_hashes):
        uid = str(self.next_uid)
        type(self).next_uid += 1
        self.messages[uid] = message_id
        if self.fail_after_append:
            type(self).fail_after_append = False
            raise RuntimeError("crash after append")
        return DraftUpdateResult(
            "personal", record.draft_id, "Drafts", "42", uid,
            message_id, attachment_hashes, "uidplus",
        )

    def expunge_superseded_draft(self, record, uid):
        persisted = self.store.get(self.draft_id, "personal")
        assert persisted.uid in self.messages
        assert persisted.superseded_uid == uid
        if self.fail_expunge:
            type(self).fail_expunge = False
            raise RuntimeError("crash during expunge")
        self.messages.pop(uid, None)


def _broker(tmp_path):
    store = FileDraftStore((tmp_path / "drafts").resolve())
    record = store.create(
        account_id="personal", mailbox="Drafts", uid_validity="42", uid="99",
        message_id="<draft@example.com>", attachment_hashes=(),
    )
    CrashSafeClient.messages = {"99": record.message_id}
    CrashSafeClient.next_uid = 100
    CrashSafeClient.fail_after_append = False
    CrashSafeClient.fail_expunge = False
    CrashSafeClient.store = store
    CrashSafeClient.draft_id = record.draft_id
    audit = Audit()
    broker = BrokerService(
        AccountRegistry([AccountConfig("personal", "mail.example", 993, "owner@example.com")]),
        Credentials(), CrashSafeClient, audit=audit, drafts=store,
    )
    return broker, store, record, audit


def _update(broker, draft_id):
    return asyncio.run(
        broker.update_draft(
            "personal", draft_id, to=("to@example.com",), subject="subject", body="body"
        )
    )


def test_new_uid_is_persisted_before_old_uid_is_expunged(tmp_path) -> None:
    broker, store, record, audit = _broker(tmp_path)
    result = _update(broker, record.draft_id)
    persisted = store.get(record.draft_id, "personal")
    assert result.uid == persisted.uid == "100"
    assert persisted.superseded_uid is None
    assert "99" not in CrashSafeClient.messages
    assert audit.events[-1].success is True


@pytest.mark.parametrize("failure", ["append", "expunge"])
def test_partial_update_failure_remains_retryable(tmp_path, failure) -> None:
    broker, store, record, audit = _broker(tmp_path)
    if failure == "append":
        CrashSafeClient.fail_after_append = True
    else:
        CrashSafeClient.fail_expunge = True
    with pytest.raises(RuntimeError, match="crash"):
        _update(broker, record.draft_id)
    persisted = store.get(record.draft_id, "personal")
    assert persisted.uid in CrashSafeClient.messages
    assert audit.events[-1].success is False

    result = _update(broker, record.draft_id)
    assert store.get(record.draft_id, "personal").uid == result.uid
    assert audit.events[-1].success is True


def test_failure_persisting_appended_uid_keeps_old_uid_retryable(tmp_path, monkeypatch) -> None:
    broker, store, record, audit = _broker(tmp_path)
    real_update = store.update
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed and kwargs.get("superseded_uid") == "99":
            failed = True
            raise OSError("crash during persist")
        return real_update(*args, **kwargs)

    monkeypatch.setattr(store, "update", fail_once)
    with pytest.raises(OSError, match="crash during persist"):
        _update(broker, record.draft_id)
    assert store.get(record.draft_id, "personal").uid == "99"
    assert "99" in CrashSafeClient.messages
    assert audit.events[-1].success is False

    result = _update(broker, record.draft_id)
    assert store.get(record.draft_id, "personal").uid == result.uid


def test_missing_uid_reconciles_by_unique_message_id_and_updates(tmp_path) -> None:
    broker, store, record, _ = _broker(tmp_path)
    CrashSafeClient.messages = {"144": record.message_id}
    result = _update(broker, record.draft_id)
    assert result.uid == "100"
    assert store.get(record.draft_id, "personal").uid == "100"


def test_missing_uid_refuses_ambiguous_message_id_without_guessing(tmp_path) -> None:
    broker, store, record, audit = _broker(tmp_path)
    CrashSafeClient.messages = {"144": record.message_id, "145": record.message_id}
    with pytest.raises(DraftProvenanceError, match="2 matching messages.*drafts repair"):
        _update(broker, record.draft_id)
    assert store.get(record.draft_id, "personal").uid == "99"
    assert audit.events[-1].success is False
