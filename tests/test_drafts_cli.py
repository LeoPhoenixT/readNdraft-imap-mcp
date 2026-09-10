from __future__ import annotations

from types import SimpleNamespace

from readndraft_imap_mcp.drafts import FileDraftStore, cli


class Accounts:
    def __init__(self, path):
        pass

    def registry(self):
        return object()


def _records(tmp_path, monkeypatch):
    directory = (tmp_path / "drafts").resolve()
    store = FileDraftStore(directory)
    records = tuple(
        store.create(
            account_id="personal", mailbox="Drafts", uid_validity="42", uid=str(uid),
            message_id=f"<draft-{uid}@example.com>", attachment_hashes=(),
        )
        for uid in (10, 20, 30)
    )
    monkeypatch.setattr(
        cli,
        "current_app_paths",
        lambda: SimpleNamespace(
            draft_dir=directory, accounts_file=tmp_path / "accounts.json"
        ),
    )
    monkeypatch.setattr(cli, "AccountFile", Accounts)
    monkeypatch.setattr(cli, "KeyringCredentialStore", lambda: object())
    return store, records


def test_drafts_forget_removes_only_local_record(tmp_path, monkeypatch, capsys) -> None:
    store, records = _records(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_matches", lambda *args: (_ for _ in ()).throw(AssertionError("IMAP called")))
    assert cli.main(["forget", "--draft-id", records[0].draft_id]) == 0
    assert {item.draft_id for item in store.list()} == {item.draft_id for item in records[1:]}
    assert "message remains in the Drafts mailbox" in capsys.readouterr().out


def test_drafts_list_reports_ok_stale_and_ambiguous(tmp_path, monkeypatch, capsys) -> None:
    _, records = _records(tmp_path, monkeypatch)
    matches = {
        records[0].draft_id: (records[0].uid,),
        records[1].draft_id: (),
        records[2].draft_id: ("31", "32"),
    }
    monkeypatch.setattr(cli, "_matches", lambda record, *args: matches[record.draft_id])
    assert cli.main(["list"]) == 0
    output = capsys.readouterr().out
    assert "\tok\n" in output
    assert "stale: tracked uid not found" in output
    assert "ambiguous: 2 matches" in output


def test_drafts_repair_is_noop_when_records_are_healthy(tmp_path, monkeypatch, capsys) -> None:
    store, records = _records(tmp_path, monkeypatch)
    before = tuple(path.read_bytes() for path in sorted(store.directory.glob("*.json")))
    monkeypatch.setattr(cli, "_matches", lambda record, *args: (record.uid,))
    assert cli.main(["repair", "--all"]) == 0
    assert tuple(path.read_bytes() for path in sorted(store.directory.glob("*.json"))) == before
    assert capsys.readouterr().out.count("already healthy") == len(records)


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


def test_drafts_list_reports_journal_recovery_even_when_record_is_healthy(tmp_path, monkeypatch, capsys) -> None:
    store, records = _records(tmp_path, monkeypatch)
    store.write_operation(records[0].draft_id, _operation(records[0]))
    monkeypatch.setattr(cli, "_operation_matches", lambda record, *args: ("91",))
    monkeypatch.setattr(cli, "_matches", lambda record, *args: (record.uid,))
    assert cli.main(["list"]) == 0
    output = capsys.readouterr().out
    assert "recovery: verified replacement uid 91" in output
    assert "operation_id=" + "b" * 32 in output
    assert f"{records[0].draft_id}\t" in output


def test_drafts_repair_refuses_absent_or_ambiguous_operation_marker(tmp_path, monkeypatch, capsys) -> None:
    store, records = _records(tmp_path, monkeypatch)
    store.write_operation(records[0].draft_id, _operation(records[0]))
    monkeypatch.setattr(cli, "_operation_matches", lambda *args: ())
    assert cli.main(["repair", "--draft-id", records[0].draft_id]) == 1
    assert store.get_operation(records[0].draft_id) is not None
    assert "recovery_required" in capsys.readouterr().out


def test_drafts_clear_operation_requires_exact_id_and_absent_marker(tmp_path, monkeypatch, capsys) -> None:
    store, records = _records(tmp_path, monkeypatch)
    record = records[0]
    operation = _operation(record)
    store.write_operation(record.draft_id, operation)
    monkeypatch.setattr(cli, "_operation_matches", lambda *args: ())
    assert cli.main([
        "repair", "--draft-id", record.draft_id, "--clear-operation", "a" * 32,
    ]) == 1
    assert store.get_operation(record.draft_id) is not None
    assert "does not match" in capsys.readouterr().out
    assert cli.main([
        "repair", "--draft-id", record.draft_id, "--clear-operation", operation["operation_id"],
    ]) == 0
    assert store.get_operation(record.draft_id) is None
    assert "cleared absent operation journal" in capsys.readouterr().out


def test_drafts_clear_operation_refuses_marker_match(tmp_path, monkeypatch, capsys) -> None:
    store, records = _records(tmp_path, monkeypatch)
    record = records[0]
    operation = _operation(record)
    store.write_operation(record.draft_id, operation)
    monkeypatch.setattr(cli, "_operation_matches", lambda *args: ("91",))
    assert cli.main([
        "repair", "--draft-id", record.draft_id, "--clear-operation", operation["operation_id"],
    ]) == 1
    assert store.get_operation(record.draft_id) is not None
    assert "refused" in capsys.readouterr().out


def test_drafts_clear_operation_refuses_legacy_journal(tmp_path, monkeypatch, capsys) -> None:
    store, records = _records(tmp_path, monkeypatch)
    record = records[0]
    operation = _operation(record)
    legacy = {
        key: value for key, value in operation.items()
        if key not in {"message_id", "attachment_hashes"}
    }
    legacy["v"] = 1
    store.write_operation(record.draft_id, legacy)
    monkeypatch.setattr(
        cli,
        "_operation_matches",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not query legacy journal")),
    )
    assert cli.main([
        "repair", "--draft-id", record.draft_id, "--clear-operation", legacy["operation_id"],
    ]) == 1
    assert store.get_operation(record.draft_id) == legacy
    assert "invalid or legacy" in capsys.readouterr().out


def test_drafts_clear_operation_refuses_provenance_mismatch_without_query(tmp_path, monkeypatch, capsys) -> None:
    store, records = _records(tmp_path, monkeypatch)
    record = records[0]
    operation = _operation(record, message_id="<other@example.com>")
    store.write_operation(record.draft_id, operation)
    monkeypatch.setattr(
        cli,
        "_operation_matches",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not query mismatched journal")),
    )
    assert cli.main([
        "repair", "--draft-id", record.draft_id, "--clear-operation", operation["operation_id"],
    ]) == 1
    assert store.get_operation(record.draft_id) == operation
    assert "invalid or legacy" in capsys.readouterr().out


def test_drafts_clear_operation_refuses_marker_lookup_failure(tmp_path, monkeypatch, capsys) -> None:
    store, records = _records(tmp_path, monkeypatch)
    record = records[0]
    operation = _operation(record)
    store.write_operation(record.draft_id, operation)
    monkeypatch.setattr(
        cli,
        "_operation_matches",
        lambda *args: (_ for _ in ()).throw(RuntimeError("IMAP unavailable")),
    )
    assert cli.main([
        "repair", "--draft-id", record.draft_id, "--clear-operation", operation["operation_id"],
    ]) == 1
    assert store.get_operation(record.draft_id) == operation
    assert "lookup failed" in capsys.readouterr().out


def test_drafts_invalid_journal_is_reported_and_refused(tmp_path, monkeypatch, capsys) -> None:
    store, records = _records(tmp_path, monkeypatch)
    (store.directory / f"{records[0].draft_id}.operation.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "_matches", lambda record, *args: (record.uid,))
    assert cli.main(["list"]) == 0
    assert "recovery_required: invalid operation journal" in capsys.readouterr().out
    assert cli.main(["repair", "--draft-id", records[0].draft_id]) == 1
    assert (store.directory / f"{records[0].draft_id}.operation.json").exists()
    assert "refused" in capsys.readouterr().out


def test_drafts_repair_adopts_verified_operation_hashes_and_clears_journal(tmp_path, monkeypatch, capsys) -> None:
    store, records = _records(tmp_path, monkeypatch)
    record = records[0]
    operation = _operation(record)
    store.write_operation(record.draft_id, operation)
    monkeypatch.setattr(cli, "_operation_matches", lambda *args: ("91",))

    class RecoveryClient:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def expunge_superseded_draft(self, replacement, old_uid):
            assert replacement.uid == "91"
            assert old_uid == record.uid

    class RecoveryAccounts:
        def require_enabled(self, account_id):
            return object()

    monkeypatch.setattr(cli, "ImapClient", RecoveryClient)
    monkeypatch.setattr(cli, "AccountFile", lambda path: SimpleNamespace(registry=lambda: RecoveryAccounts()))
    monkeypatch.setattr(cli, "KeyringCredentialStore", lambda: SimpleNamespace(load_secret=lambda _: _secret()))
    assert cli.main(["repair", "--draft-id", record.draft_id]) == 0
    recovered = store.get(record.draft_id, record.account_id)
    assert recovered.uid == "91"
    assert recovered.attachment_hashes == ("c" * 64,)
    assert recovered.superseded_uid is None
    assert store.get_operation(record.draft_id) is None
    assert "recovered" in capsys.readouterr().out


async def _secret():
    return "secret"
