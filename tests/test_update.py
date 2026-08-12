from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import readndraft_imap_mcp.platform.update as update_module
from readndraft_imap_mcp.platform.client_config import codex_config
from readndraft_imap_mcp.platform.paths import AppPaths
from readndraft_imap_mcp.platform.skill import SKILL_NAMES, install_skill
from readndraft_imap_mcp.platform.update import apply_update, inspect_client


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        (tmp_path / "config").resolve(),
        (tmp_path / "state").resolve(),
        (tmp_path / "runtime").resolve(),
    )


def _uvw(tmp_path: Path) -> Path:
    executable = (tmp_path / "tools" / "uvw.exe").resolve()
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"placeholder")
    return executable


def _codex_entry(command: Path, version: str) -> str:
    return codex_config(
        str(command),
        ["tool", "run", f"readndraft-imap-mcp@{version}", "mcp"],
        startup_timeout=30,
    )


def test_check_is_read_only_and_reports_missing_skills(tmp_path) -> None:
    paths = _paths(tmp_path)
    home = (tmp_path / "home").resolve()
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(_codex_entry(_uvw(tmp_path), "0.1.9"), encoding="utf-8")
    before = config.read_bytes()

    status = inspect_client("codex", paths=paths, home=home, target_version="0.2.0")

    assert status.mcp == "outdated"
    assert status.installed_version == "0.1.9"
    assert status.skills == {name: "not installed" for name in SKILL_NAMES}
    assert status.restart_required is True
    assert config.read_bytes() == before
    assert not paths.state_dir.exists()


def test_codex_update_replaces_only_managed_table_and_installs_both_skills(
    tmp_path, monkeypatch
) -> None:
    paths = _paths(tmp_path)
    home = (tmp_path / "home").resolve()
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    uvw = _uvw(tmp_path)
    config.write_text(
        'model = "example"\n# keep this comment\n\n'
        + _codex_entry(uvw, "0.1.9")
        + '[projects."C:/work"]\ntrusted = true\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        update_module,
        "uvx_invocation",
        lambda version=None: (
            str(uvw),
            ["tool", "run", f"readndraft-imap-mcp@{version}", "mcp"],
        ),
    )

    status = apply_update(
        "codex", paths=paths, home=home, target_version="0.2.0"
    )

    text = config.read_text(encoding="utf-8")
    assert 'model = "example"\n# keep this comment' in text
    assert '[projects."C:/work"]\ntrusted = true' in text
    assert "readndraft-imap-mcp@0.2.0" in text
    assert "readndraft-imap-mcp@0.1.9" not in text
    assert status.mcp == "current"
    assert status.skills == {name: "current" for name in SKILL_NAMES}
    assert len(list((paths.state_dir / "update-backups").glob("*-codex-config.toml"))) == 1


def test_modified_skill_blocks_normal_update_and_force_replaces_it(
    tmp_path, monkeypatch
) -> None:
    paths = _paths(tmp_path)
    home = (tmp_path / "home").resolve()
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    uvw = _uvw(tmp_path)
    config.write_text(_codex_entry(uvw, "0.2.0"), encoding="utf-8")
    target = install_skill("codex", paths=paths, home=home)
    (target / "SKILL.md").write_text("local modification", encoding="utf-8")
    monkeypatch.setattr(
        update_module,
        "uvx_invocation",
        lambda version=None: (
            str(uvw),
            ["tool", "run", f"readndraft-imap-mcp@{version}", "mcp"],
        ),
    )

    with pytest.raises(RuntimeError, match="--force-skill"):
        apply_update("codex", paths=paths, home=home, target_version="0.2.0")
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "local modification"

    status = apply_update(
        "codex",
        paths=paths,
        home=home,
        target_version="0.2.0",
        force_skill=True,
    )
    assert status.skills == {name: "current" for name in SKILL_NAMES}
    assert "local modification" not in (target / "SKILL.md").read_text(encoding="utf-8")


def test_unrecognized_mcp_entry_is_never_force_replaced(tmp_path) -> None:
    paths = _paths(tmp_path)
    home = (tmp_path / "home").resolve()
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    original = '[mcp_servers.readndraft]\ncommand = "custom-wrapper"\n'
    config.write_text(original, encoding="utf-8")

    with pytest.raises(RuntimeError, match="unrecognized"):
        apply_update(
            "codex",
            paths=paths,
            home=home,
            target_version="0.2.0",
            force_skill=True,
        )
    assert config.read_text(encoding="utf-8") == original


def test_current_installation_is_a_noop_without_backups(tmp_path) -> None:
    paths = _paths(tmp_path)
    home = (tmp_path / "home").resolve()
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(_codex_entry(_uvw(tmp_path), "0.2.0"), encoding="utf-8")
    for name in SKILL_NAMES:
        install_skill("codex", paths=paths, home=home, skill_name=name)
    before = config.read_bytes()

    status = apply_update(
        "codex", paths=paths, home=home, target_version="0.2.0"
    )

    assert status.restart_required is False
    assert config.read_bytes() == before
    assert not (paths.state_dir / "update-backups").exists()


def test_claude_local_scope_entry_blocks_user_update(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    home = (tmp_path / "home").resolve()
    configuration = home / ".claude.json"
    configuration.parent.mkdir(parents=True)
    configuration.write_text(
        json.dumps(
            {
                "projects": {
                    "C:/work": {
                        "mcpServers": {
                            "readndraft": {"command": "custom", "args": []}
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(update_module, "_project_claude_entry_present", lambda cwd=None: False)

    status = inspect_client(
        "claude-code", paths=paths, home=home, target_version="0.2.0"
    )
    assert status.mcp == "unrecognized"
    with pytest.raises(RuntimeError, match="unrecognized"):
        apply_update(
            "claude-code", paths=paths, home=home, target_version="0.2.0"
        )


def test_claude_update_uses_direct_user_scope_arguments(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    home = (tmp_path / "home").resolve()
    configuration = home / ".claude.json"
    configuration.parent.mkdir(parents=True)
    uvw = _uvw(tmp_path)
    calls: list[list[str]] = []

    monkeypatch.setattr(
        update_module,
        "uvx_invocation",
        lambda version=None: (
            str(uvw),
            ["tool", "run", f"readndraft-imap-mcp@{version}", "mcp"],
        ),
    )

    def runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        payload = json.loads(arguments[4])
        configuration.write_text(
            json.dumps({"mcpServers": {"readndraft": payload}}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(arguments, 0, "", "")

    status = apply_update(
        "claude-code",
        paths=paths,
        home=home,
        target_version="0.2.0",
        runner=runner,
    )

    assert calls == [[
        "claude",
        "mcp",
        "add-json",
        "readndraft",
        calls[0][4],
        "--scope",
        "user",
    ]]
    assert status.mcp == "current"
    assert status.skills == {name: "current" for name in SKILL_NAMES}


def test_failed_claude_add_rolls_back_configuration_and_skills(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    home = (tmp_path / "home").resolve()
    configuration = home / ".claude.json"
    configuration.parent.mkdir(parents=True)
    uvw = _uvw(tmp_path)
    original = {
        "mcpServers": {
            "readndraft": {
                "type": "stdio",
                "command": str(uvw),
                "args": ["tool", "run", "readndraft-imap-mcp@0.1.9", "mcp"],
                "env": {},
            }
        },
        "keep": True,
    }
    configuration.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(
        update_module,
        "uvx_invocation",
        lambda version=None: (
            str(uvw),
            ["tool", "run", f"readndraft-imap-mcp@{version}", "mcp"],
        ),
    )

    def failing_runner(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        if "remove" in arguments:
            configuration.write_text(json.dumps({"keep": True}), encoding="utf-8")
            return subprocess.CompletedProcess(arguments, 0, "", "")
        return subprocess.CompletedProcess(arguments, 1, "", "failed")

    with pytest.raises(RuntimeError, match="could not add"):
        apply_update(
            "claude-code",
            paths=paths,
            home=home,
            target_version="0.2.0",
            runner=failing_runner,
        )

    assert json.loads(configuration.read_text(encoding="utf-8")) == original
    for name in SKILL_NAMES:
        assert not (home / ".claude" / "skills" / name).exists()
