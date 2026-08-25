from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from readndraft_imap_mcp.cli import main
from readndraft_imap_mcp.platform.paths import AppPaths
from readndraft_imap_mcp.platform.setup import (
    _print_client_setup,
    run_setup,
)
from readndraft_imap_mcp.platform.setup import (
    main as setup_main,
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
    for command in (
        "setup",
        "account",
        "configure",
        "doctor",
        "migrate-plugin",
        "attachments",
        "mcp",
    ):
        assert command in output
    assert "  skill " not in output
    assert "  update " not in output


def test_top_level_broker_stop_is_dispatched(monkeypatch) -> None:
    from readndraft_imap_mcp.broker import daemon

    stops = []
    monkeypatch.setattr(daemon, "_stop_broker", lambda: stops.append(True) or 0)
    assert main(["broker", "stop"]) == 0
    assert stops == [True]


@pytest.mark.parametrize("command", ["skill", "update"])
def test_removed_compatibility_commands_are_unknown(command, capsys) -> None:
    assert main([command]) == 2
    assert f"Unknown command: {command}" in capsys.readouterr().err


def test_removed_setup_install_skill_option_is_rejected() -> None:
    with pytest.raises(SystemExit, match="2"):
        setup_main(["--install-skill"])


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


def test_setup_points_codex_to_native_plugin(capsys) -> None:
    _print_client_setup("codex")
    output = capsys.readouterr().out
    assert "marketplace plugin supplies the MCP server and email skill" in output
    assert "client's native plugin manager" in output
    assert "COPY START" not in output
    assert "uvx readndraft-imap-mcp@latest doctor --online" in output
