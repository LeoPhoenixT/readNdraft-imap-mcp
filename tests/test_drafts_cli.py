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
