from __future__ import annotations

import json
import os
import shutil
import tomllib

import pytest

import readndraft_imap_mcp.platform.client_config as client_config_module
from readndraft_imap_mcp.platform.client_config import (
    claude_code_config,
    client_config,
    codex_config,
    main,
    resolve_command,
    uvx_invocation,
)


def test_codex_and_chatgpt_shared_config_is_secret_free(tmp_path) -> None:
    command = str((tmp_path / "readndraft-mcp").resolve())
    parsed = tomllib.loads(codex_config(command))
    server = parsed["mcp_servers"]["readndraft"]

    assert server["command"] == command
    assert server["required"] is True
    assert server["default_tools_approval_mode"] == "approve"
    assert "env" not in server
    assert "secret" not in repr(server).casefold()


def test_claude_code_config_is_stdio_and_secret_free(tmp_path) -> None:
    command = str((tmp_path / "readndraft-mcp").resolve())
    server = json.loads(claude_code_config(command))

    assert server == {"type": "stdio", "command": command, "args": [], "env": {}}


def test_command_resolution_requires_existing_file(tmp_path) -> None:
    executable = (tmp_path / "readndraft-mcp").resolve()
    executable.write_text("placeholder", encoding="utf-8")

    assert resolve_command(str(executable)) == str(executable)
    with pytest.raises(ValueError, match="existing absolute"):
        resolve_command(str(tmp_path / "missing"))


@pytest.mark.parametrize("client", ["codex", "chatgpt-desktop", "claude-code"])
def test_cli_and_programmatic_config_are_equivalent(client, tmp_path, capsys) -> None:
    executable = (tmp_path / "readndraft-mcp").resolve()
    executable.write_text("placeholder", encoding="utf-8")

    expected = client_config(client, uvx=False, command=str(executable))
    assert main([client, "--command", str(executable)]) == 0

    assert capsys.readouterr().out == expected


def test_on_demand_launcher_is_installed_and_configurable() -> None:
    launcher = shutil.which("readndraft-launch")
    assert launcher is not None
    assert os.path.normcase(resolve_command(executable="readndraft-launch")) == os.path.normcase(launcher)
    assert json.loads(claude_code_config(launcher))["command"] == launcher


def test_uvx_config_is_version_pinned_and_allows_startup_download(monkeypatch) -> None:
    monkeypatch.setattr(
        "readndraft_imap_mcp.platform.client_config.uvx_invocation",
        lambda version=None: ("/tools/uvx", ["readndraft-imap-mcp@0.2.0", "mcp"]),
    )

    parsed = tomllib.loads(client_config("codex", uvx=True))
    server = parsed["mcp_servers"]["readndraft"]
    assert server["command"] == "/tools/uvx"
    assert server["args"] == ["readndraft-imap-mcp@0.2.0", "mcp"]
    assert server["startup_timeout_sec"] == 30
    assert "env" not in server

    claude = json.loads(client_config("claude-code", uvx=True))
    assert claude["args"] == ["readndraft-imap-mcp@0.2.0", "mcp"]


def test_windows_uvx_config_uses_consoleless_uvw(monkeypatch, tmp_path) -> None:
    uvw = (tmp_path / "uvw.exe").resolve()
    uvw.write_bytes(b"placeholder")
    monkeypatch.setattr(client_config_module.sys, "platform", "win32")
    monkeypatch.setattr(
        client_config_module,
        "resolve_command",
        lambda value=None, *, executable="readndraft-mcp": str(uvw)
        if executable == "uvw"
        else pytest.fail(f"unexpected executable: {executable}"),
    )

    assert uvx_invocation("0.2.0") == (
        str(uvw),
        ["tool", "run", "readndraft-imap-mcp@0.2.0", "mcp"],
    )
