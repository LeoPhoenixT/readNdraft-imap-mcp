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
