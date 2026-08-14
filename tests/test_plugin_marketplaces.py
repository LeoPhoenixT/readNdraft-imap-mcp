from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from readndraft_imap_mcp.platform.paths import AppPaths
from readndraft_imap_mcp.platform.plugin_migration import migrate_plugin
from readndraft_imap_mcp.platform.skill import SKILL_NAMES, install_all_skills


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "readndraft"


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_shared_plugin_and_marketplaces_are_self_contained_and_version_locked() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    codex_plugin = _json(PLUGIN / ".codex-plugin" / "plugin.json")
    claude_plugin = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    codex_marketplace = _json(ROOT / ".agents" / "plugins" / "marketplace.json")
    claude_marketplace = _json(ROOT / ".claude-plugin" / "marketplace.json")
    mcp = _json(PLUGIN / ".mcp.json")

    assert codex_plugin["name"] == claude_plugin["name"] == "readndraft"
    assert codex_marketplace["plugins"][0]["name"] == "readndraft"
    assert claude_marketplace["plugins"][0]["name"] == "readndraft"
    version = project["project"]["version"]
    assert version == codex_plugin["version"] == claude_plugin["version"]
    assert version == claude_marketplace["plugins"][0]["version"]
    server = mcp["mcpServers"]["readndraft"]
    assert server["command"] == "uv"
    assert server["args"] == [
        "tool",
        "run",
        f"readndraft-imap-mcp@{version}",
        "mcp",
    ]
    assert (PLUGIN / "skills" / "readndraft-email" / "SKILL.md").is_file()
    assert (PLUGIN / "LICENSE").is_file()

    rendered = json.dumps(
        [codex_plugin, claude_plugin, codex_marketplace, claude_marketplace, mcp]
    ).casefold()
    assert "password" not in rendered
    assert "smtp" not in rendered
    assert "send email" not in rendered


def test_marketplace_sources_point_only_to_shared_plugin() -> None:
    codex = _json(ROOT / ".agents" / "plugins" / "marketplace.json")
    claude = _json(ROOT / ".claude-plugin" / "marketplace.json")
    assert codex["plugins"][0]["source"] == {
        "source": "local",
        "path": "./plugins/readndraft",
    }
    assert claude["plugins"][0]["source"] == "./plugins/readndraft"


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "runtime")


def _completed(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, "", "")


def _legacy_invocation() -> tuple[str, list[str]]:
    package = "readndraft-imap-mcp@0.3.0"
    if sys.platform == "win32":
        return r"C:\bin\uvw.exe", ["tool", "run", package, "mcp"]
    return "/usr/bin/uvx", [package, "mcp"]


def _codex_legacy_config() -> str:
    command, args = _legacy_invocation()
    return (
        "[mcp_servers.readndraft]\n"
        f"command = {json.dumps(command)}\n"
        f"args = {json.dumps(args)}\n"
    )


def test_migration_refuses_unknown_mcp_without_mutation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    original = '[mcp_servers.readndraft]\ncommand = "custom-mail-wrapper"\n'
    config.write_text(original, encoding="utf-8")
    calls: list[list[str]] = []

    with pytest.raises(RuntimeError, match="unknown/custom"):
        migrate_plugin(
            "codex",
            paths=_paths(tmp_path),
            home=home,
            runner=lambda command: calls.append(command) or _completed(command),
        )

    assert config.read_text(encoding="utf-8") == original
    assert calls == []


def test_migration_refuses_modified_managed_skill_before_mcp_removal(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths = _paths(tmp_path)
    targets = install_all_skills("codex", paths=paths, home=home)
    (targets[0] / "SKILL.md").write_text("user modification\n", encoding="utf-8")
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(_codex_legacy_config(), encoding="utf-8")
    calls: list[list[str]] = []

    with pytest.raises(RuntimeError, match="modified or unmanaged"):
        migrate_plugin(
            "codex",
            paths=paths,
            home=home,
            runner=lambda command: calls.append(command) or _completed(command),
        )

    assert calls == []
    assert targets[0].is_dir()


def test_migration_removes_only_recognized_mcp_and_clean_skills(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths = _paths(tmp_path)
    targets = install_all_skills("codex", paths=paths, home=home)
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(_codex_legacy_config(), encoding="utf-8")

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        config.write_text("", encoding="utf-8")
        return _completed(command)

    result = migrate_plugin("codex", paths=paths, home=home, runner=runner)
    assert result.mcp_removed is True
    assert result.skills_removed == SKILL_NAMES
    assert all(not target.exists() for target in targets)
    assert not (paths.state_dir / "skill-installs.json").exists()

    second = migrate_plugin("codex", paths=paths, home=home, runner=runner)
    assert second.mcp_removed is False
    assert second.skills_removed == ()


def test_claude_migration_uses_native_user_scope_removal(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths = _paths(tmp_path)
    config = home / ".claude.json"
    config.parent.mkdir(parents=True)
    command, args = _legacy_invocation()
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "readndraft": {
                        "type": "stdio",
                        "command": command,
                        "args": args,
                    }
                },
                "projects": {},
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        config.write_text('{"mcpServers": {}, "projects": {}}\n', encoding="utf-8")
        return _completed(command)

    result = migrate_plugin(
        "claude-code",
        paths=paths,
        home=home,
        cwd=tmp_path,
        runner=runner,
    )
    assert result.mcp_removed is True
    assert calls == [["claude", "mcp", "remove", "readndraft", "--scope", "user"]]
