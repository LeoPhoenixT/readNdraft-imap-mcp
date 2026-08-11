from __future__ import annotations

import json
import os

import pytest

from readndraft_imap_mcp.admin import AccountFile
from readndraft_imap_mcp.broker import AccountConfig


def test_account_file_round_trip_contains_metadata_but_no_secret(tmp_path) -> None:
    path = (tmp_path / "config" / "accounts.json").resolve()
    accounts = AccountFile(path)

    accounts.upsert(
        AccountConfig("personal", "imap.example.com", 993, "leo@example.com")
    )

    assert accounts.load()[0].hostname == "imap.example.com"
    raw = path.read_text(encoding="utf-8")
    assert json.loads(raw)[0]["username"] == "leo@example.com"
    assert "password" not in raw.casefold()
    assert "secret" not in raw.casefold()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_account_file_enable_disable_and_delete(tmp_path) -> None:
    accounts = AccountFile((tmp_path / "accounts.json").resolve())
    accounts.upsert(AccountConfig("work", "imap.example.com", 993, "a@example.com"))

    accounts.set_enabled("work", False)
    with pytest.raises(PermissionError, match="disabled"):
        accounts.registry().require_enabled("work")
    accounts.set_enabled("work", True)
    assert accounts.registry().require_enabled("work").account_id == "work"
    accounts.delete("work")
    assert accounts.load() == ()


def test_account_file_rejects_unknown_fields(tmp_path) -> None:
    path = (tmp_path / "accounts.json").resolve()
    path.write_text(
        '[{"account_id":"work","hostname":"imap.example.com","port":993,'
        '"username":"a@example.com","password":"nope"}]',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid account configuration"):
        AccountFile(path).load()
