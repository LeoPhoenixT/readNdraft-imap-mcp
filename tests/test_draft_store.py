from __future__ import annotations

import json
import os

import pytest

from readndraft_imap_mcp.drafts import FileDraftStore
from readndraft_imap_mcp.drafts.store import DraftProvenanceError


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
