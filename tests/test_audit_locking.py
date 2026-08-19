from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import readndraft_imap_mcp.admin.cli as admin_cli
from readndraft_imap_mcp.audit import AuditEvent, JsonlAuditSink
from readndraft_imap_mcp.platform.paths import AppPaths


def _event(operation: str) -> AuditEvent:
    return AuditEvent.mutation(
        operation="set_star",
        account_id="personal",
        mailbox="INBOX",
        uid=operation,
        success=True,
        duration_ms=1,
    )


def _write_fork(path: Path) -> None:
    sink = JsonlAuditSink(path)
    sink._record_sync(_event("op1"))
    sink._record_sync(_event("op2"))
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    fork = {**_event("op3").to_dict(), "previous_hash": records[0]["entry_hash"]}
    fork["entry_hash"] = sink._hash(fork)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(fork, sort_keys=True, separators=(",", ":")) + "\n")


def _paths(tmp_path: Path) -> AppPaths:
    paths = AppPaths(
        (tmp_path / "config").resolve(),
        (tmp_path / "state").resolve(),
        (tmp_path / "runtime").resolve(),
    )
    paths.ensure_private()
    return paths


def test_interleaved_sinks_keep_chain_intact(tmp_path: Path) -> None:
    path = (tmp_path / "audit.jsonl").resolve()
    first, second = JsonlAuditSink(path), JsonlAuditSink(path)
    for index in range(10):
        sink = first if index % 2 == 0 else second
        sink._record_sync(_event(f"op{index}"))
    assert JsonlAuditSink(path).verify_sync() == 10


def test_concurrent_sinks_keep_chain_intact(tmp_path: Path) -> None:
    path = (tmp_path / "audit.jsonl").resolve()
    sinks = [JsonlAuditSink(path) for _ in range(4)]

    def append(index: int) -> None:
        sinks[index % 4]._record_sync(_event(f"op{index}"))

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(append, range(100)))
    assert JsonlAuditSink(path).verify_sync() == 100


def test_verifier_identifies_concurrent_writer_fork(tmp_path: Path) -> None:
    path = (tmp_path / "audit.jsonl").resolve()
    _write_fork(path)
    with pytest.raises(RuntimeError, match="concurrent-writer fork at line 3"):
        JsonlAuditSink(path).verify_sync()


def test_audit_repair_leaves_healthy_log_untouched(monkeypatch, tmp_path: Path, capsys) -> None:
    paths = _paths(tmp_path)
    JsonlAuditSink(paths.audit_file)._record_sync(_event("op1"))
    original = paths.audit_file.read_bytes()
    monkeypatch.setattr(admin_cli, "current_app_paths", lambda: paths)

    assert admin_cli.main(["audit", "repair"]) == 0
    assert paths.audit_file.read_bytes() == original
    assert "Nothing to repair" in capsys.readouterr().out


def test_audit_repair_archives_forked_log(monkeypatch, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_fork(paths.audit_file)
    monkeypatch.setattr(admin_cli, "current_app_paths", lambda: paths)

    assert admin_cli.main(["audit", "repair"]) == 0
    assert not paths.audit_file.exists()
    assert len(list(paths.state_dir.glob("audit.jsonl.corrupt-*"))) == 1
