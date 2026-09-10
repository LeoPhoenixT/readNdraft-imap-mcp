from __future__ import annotations

import json
import os

import pytest

from readndraft_imap_mcp.drafts import FileDraftStore
from readndraft_imap_mcp.drafts.store import (
    DraftBusyError,
    DraftProvenanceError,
    DraftRecoveryRequiredError,
)


def _operation(record, **changes):
    return {
        "v": 2,
        "operation_id": "b" * 32,
        "account_id": record.account_id,
        "mailbox": record.mailbox,
        "uid_validity": record.uid_validity,
        "old_uid": record.uid,
        "message_id": record.message_id,
        "attachment_hashes": ["c" * 64],
        "phase": "prepared",
        **changes,
    }


def test_draft_provenance_is_private_and_account_pinned(tmp_path) -> None:
    store = FileDraftStore((tmp_path / "drafts").resolve())
    record = store.create(
        account_id="personal",
        mailbox="Drafts",
        uid_validity="42",
        uid="99",
        message_id="<draft@example.com>",
        attachment_hashes=("a" * 64,),
    )
    assert store.get(record.draft_id, "personal") == record
    with pytest.raises(DraftProvenanceError, match="another account"):
        store.get(record.draft_id, "work")
    path = store.directory / f"{record.draft_id}.json"
    assert "recipient" not in path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_concurrent_provenance_change_is_rejected(tmp_path) -> None:
    store = FileDraftStore((tmp_path / "drafts").resolve())
    record = store.create(
        account_id="personal",
        mailbox="Drafts",
        uid_validity="42",
        uid="99",
        message_id="<draft@example.com>",
        attachment_hashes=(),
    )
    path = store.directory / f"{record.draft_id}.json"
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["uid"] = "100"
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(DraftProvenanceError, match="changed concurrently"):
        store.update(
            record,
            mailbox="Drafts",
            uid_validity="42",
            uid="101",
            message_id=record.message_id,
            attachment_hashes=(),
        )


def test_legacy_provenance_loads_without_threading_metadata(tmp_path) -> None:
    store = FileDraftStore((tmp_path / "drafts").resolve())
    record = store.create(
        account_id="personal", mailbox="Drafts", uid_validity="42", uid="99",
        message_id="<draft@example.com>", attachment_hashes=(),
    )
    path = store.directory / f"{record.draft_id}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value.pop("in_reply_to")
    value.pop("references")
    path.write_text(json.dumps(value), encoding="utf-8")
    loaded = store.get(record.draft_id, "personal")
    assert loaded.in_reply_to is None
    assert loaded.references == ()


def test_separate_store_instances_reject_concurrent_draft_operation(tmp_path) -> None:
    first = FileDraftStore((tmp_path / "drafts").resolve())
    record = first.create(
        account_id="personal", mailbox="Drafts", uid_validity="42", uid="99",
        message_id="<draft@example.com>", attachment_hashes=(),
    )
    second = FileDraftStore(first.directory)
    with first.operation_lock(record.draft_id):
        with pytest.raises(DraftBusyError):
            with second.operation_lock(record.draft_id):
                raise AssertionError("second update acquired the lock")


def test_provenance_write_is_fsynced_and_interrupted_replace_keeps_old_json(tmp_path, monkeypatch) -> None:
    store = FileDraftStore((tmp_path / "drafts").resolve())
    record = store.create(
        account_id="personal", mailbox="Drafts", uid_validity="42", uid="99",
        message_id="<draft@example.com>", attachment_hashes=(),
    )
    path = store.directory / f"{record.draft_id}.json"
    original = path.read_bytes()
    calls = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "replace", lambda source, target: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError, match="crash"):
        store.update(
            record, mailbox="Drafts", uid_validity="42", uid="100",
            message_id=record.message_id, attachment_hashes=(), superseded_uid="99",
        )

    assert calls
    assert path.read_bytes() == original
    assert json.loads(path.read_text(encoding="utf-8"))["uid"] == "99"
    assert not tuple(store.directory.glob("*.tmp"))


def test_operation_validation_is_versioned_and_fails_closed(tmp_path) -> None:
    store = FileDraftStore((tmp_path / "drafts").resolve())
    record = store.create(
        account_id="personal", mailbox="Drafts", uid_validity="42", uid="99",
        message_id="<draft@example.com>", attachment_hashes=(),
    )
    store.write_operation(record.draft_id, _operation(record))
    operation = store.get_operation(record.draft_id)
    assert operation is not None
    assert store.validate_operation(record, operation) == ("c" * 64,)
    store.write_operation(record.draft_id, _operation(record, message_id="<other@example.com>"))
    with pytest.raises(DraftRecoveryRequiredError):
        store.validate_operation(record, store.get_operation(record.draft_id) or {})


def test_forget_removes_associated_operation_journal(tmp_path) -> None:
    store = FileDraftStore((tmp_path / "drafts").resolve())
    record = store.create(
        account_id="personal", mailbox="Drafts", uid_validity="42", uid="99",
        message_id="<draft@example.com>", attachment_hashes=(),
    )
    store.write_operation(record.draft_id, _operation(record))
    with store.operation_lock(record.draft_id):
        assert store.forget(record.draft_id)
    assert not (store.directory / f"{record.draft_id}.json").exists()
    assert store.get_operation(record.draft_id) is None
