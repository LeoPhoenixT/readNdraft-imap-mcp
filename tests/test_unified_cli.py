from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from readndraft_imap_mcp.cli import main
from readndraft_imap_mcp.platform.paths import AppPaths
from readndraft_imap_mcp.platform.setup import run_setup
from readndraft_imap_mcp.platform.skill import (
    install_skill,
    skill_status,
    uninstall_skill,
)


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        (tmp_path / "config").resolve(),
        (tmp_path / "state").resolve(),
        (tmp_path / "runtime").resolve(),
    )


class FakeCredentials:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def save_secret(self, account_id: str, secret: str) -> None:
        self.values[account_id] = secret

    async def delete_secret(self, account_id: str) -> None:
        self.values.pop(account_id, None)


class FakeClient:
    def __init__(self, account, secret) -> None:
        assert account.hostname == "imap.example.com"
        assert secret == "app-password"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def list_mailboxes(self):
        return ("INBOX", "Drafts")


def test_unified_help_has_public_commands(capsys) -> None:
    assert main(["--help"]) == 0
    output = capsys.readouterr().out
    for command in ("setup", "account", "configure", "doctor", "skill", "attachments", "mcp"):
        assert command in output


def test_setup_authenticates_then_persists_secret_and_metadata(tmp_path) -> None:
    answers = iter(("work", "imap.example.com", "993", "leo@example.com", "login"))
    credentials = FakeCredentials()
    account, count = asyncio.run(
        run_setup(
            _paths(tmp_path),
            input_fn=lambda prompt: next(answers),
            secret_fn=lambda prompt: "app-password",
            credentials=credentials,
            client_factory=FakeClient,
            require_backend=lambda: object(),
        )
    )

    assert account.account_id == "work"
    assert count == 2
    assert credentials.values == {"work": "app-password"}
    metadata = json.loads(_paths(tmp_path).accounts_file.read_text(encoding="utf-8"))
    assert metadata[0]["username"] == "leo@example.com"
    assert "password" not in repr(metadata).casefold()


def test_skill_install_is_managed_and_refuses_modified_removal(tmp_path) -> None:
    paths = _paths(tmp_path)
    home = (tmp_path / "home").resolve()
    target = install_skill("codex", paths=paths, home=home)
    assert target == home / ".agents" / "skills" / "readndraft-email"
    assert (target / "SKILL.md").is_file()

    (target / "SKILL.md").write_text("modified", encoding="utf-8")
    with pytest.raises(RuntimeError, match="modified"):
        uninstall_skill("codex", paths=paths, home=home)


def test_skill_install_and_clean_uninstall(tmp_path) -> None:
    paths = _paths(tmp_path)
    home = (tmp_path / "home").resolve()
    target = install_skill("claude-code", paths=paths, home=home)
    uninstall_skill("claude-code", paths=paths, home=home)
    assert not target.exists()


def test_forced_skill_upgrade_removes_stale_orphan_files(tmp_path) -> None:
    paths = _paths(tmp_path)
    home = (tmp_path / "home").resolve()
    target = install_skill("claude-code", paths=paths, home=home)
    orphan = target / "references" / "approvals-and-errors.md"
    orphan.write_text("stale approval workflow", encoding="utf-8")

    assert skill_status("claude-code", paths=paths, home=home)[0] == "modified"
    install_skill("claude-code", paths=paths, home=home, force=True)

    assert not orphan.exists()
    assert skill_status("claude-code", paths=paths, home=home)[0] == "current"
