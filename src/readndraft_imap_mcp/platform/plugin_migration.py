from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ._utils import run as _run
from .paths import AppPaths, current_app_paths

CLIENTS = ("codex", "claude-code")
LEGACY_SKILL_NAMES = ("readndraft-email", "readndraft-update")
_DEFAULT_SKILL_NAME = LEGACY_SKILL_NAMES[0]
_PIN = re.compile(r"^readndraft-imap-mcp@([^\s]+)$")


@dataclass(frozen=True, slots=True)
class MigrationResult:
    client: str
    mcp_removed: bool
    skills_removed: tuple[str, ...]


def _digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _manifest_path(paths: AppPaths) -> Path:
    return paths.state_dir / "skill-installs.json"


def _load_manifest(paths: AppPaths) -> dict[str, dict[str, str]]:
    try:
        value = json.loads(_manifest_path(paths).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError("invalid legacy skill installation manifest")
    return value


def _manifest_key(client: str, name: str) -> str:
    return f"{client}:{name}"


def _manifest_record(
    manifest: dict[str, dict[str, str]], client: str, name: str
) -> dict[str, str] | None:
    record = manifest.get(_manifest_key(client, name))
    if record is None and name == _DEFAULT_SKILL_NAME:
        record = manifest.get(client)
    return record if isinstance(record, dict) else None


def _save_manifest(paths: AppPaths, manifest: dict[str, dict[str, str]]) -> None:
    target = _manifest_path(paths)
    if not manifest:
        target.unlink(missing_ok=True)
        return
    paths.ensure_private()
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if os.name != "nt":
        os.chmod(temporary, 0o600)
    os.replace(temporary, target)


def _skill_destination(client: str, home: Path, name: str) -> Path:
    if client == "codex":
        return home / ".agents" / "skills" / name
    return home / ".claude" / "skills" / name


def _skill_state(
    client: str, name: str, *, paths: AppPaths, home: Path
) -> str:
    target = _skill_destination(client, home, name)
    if not target.exists():
        return "not installed"
    if not target.is_dir():
        return "unmanaged"
    record = _manifest_record(_load_manifest(paths), client, name)
    if record is None or record.get("path") != str(target):
        return "unmanaged"
    return "managed" if record.get("hash") == _digest(target) else "modified"


def _pin_from_args(args: object) -> str | None:
    if not isinstance(args, list):
        return None
    matches = [
        match.group(1)
        for value in args
        if isinstance(value, str)
        if (match := _PIN.match(value))
    ]
    return matches[0] if len(matches) == 1 and args[-1:] == ["mcp"] else None


def _recognized_invocation(command: object, args: object) -> bool:
    if not isinstance(command, str) or not command:
        return False
    executable = Path(command).name.casefold()
    if executable not in {"uvx", "uvx.exe", "uvw", "uvw.exe"}:
        return False
    version = _pin_from_args(args)
    if version is None:
        return False
    expected = [f"readndraft-imap-mcp@{version}", "mcp"]
    if executable in {"uvw", "uvw.exe"}:
        expected = ["tool", "run", *expected]
    return args == expected


def _read_codex_server(home: Path) -> str:
    path = home / ".codex" / "config.toml"
    if not path.is_file():
        return "not installed"
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "unrecognized"
    server = document.get("mcp_servers", {}).get("readndraft")
    if server is None:
        return "not installed"
    if not isinstance(server, dict):
        return "unrecognized"
    return (
        "recognized"
        if _recognized_invocation(server.get("command"), server.get("args"))
        else "unrecognized"
    )


def _project_claude_entry_present(cwd: Path | None) -> bool:
    path = (cwd or Path.cwd()) / ".mcp.json"
    if not path.is_file():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    servers = document.get("mcpServers", {})
    return not isinstance(servers, dict) or "readndraft" in servers


def _read_claude_server(home: Path) -> str:
    path = home / ".claude.json"
    if not path.is_file():
        return "not installed"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unrecognized"
    servers = document.get("mcpServers", {})
    projects = document.get("projects", {})
    if not isinstance(servers, dict) or not isinstance(projects, dict):
        return "unrecognized"
    for project in projects.values():
        if isinstance(project, dict):
            local_servers = project.get("mcpServers", {})
            if isinstance(local_servers, dict) and "readndraft" in local_servers:
                return "unrecognized"
    server = servers.get("readndraft")
    if server is None:
        return "not installed"
    if not isinstance(server, dict):
        return "unrecognized"
    return (
        "recognized"
        if _recognized_invocation(server.get("command"), server.get("args"))
        else "unrecognized"
    )


def _inspect_mcp(client: str, home: Path, cwd: Path | None) -> str:
    if client == "codex":
        return _read_codex_server(home)
    if _project_claude_entry_present(cwd):
        return "unrecognized"
    return _read_claude_server(home)


def _remove_managed_skill(
    client: str, name: str, *, paths: AppPaths, home: Path
) -> None:
    target = _skill_destination(client, home, name)
    manifest = _load_manifest(paths)
    record = _manifest_record(manifest, client, name)
    if (
        not target.is_dir()
        or record is None
        or record.get("path") != str(target)
        or record.get("hash") != _digest(target)
    ):
        raise RuntimeError(f"{name} is not a confirmed unmodified managed skill")
    shutil.rmtree(target)
    manifest.pop(_manifest_key(client, name), None)
    if name == _DEFAULT_SKILL_NAME:
        manifest.pop(client, None)
    _save_manifest(paths, manifest)


def migrate_plugin(
    client: str,
    *,
    paths: AppPaths,
    home: Path | None = None,
    cwd: Path | None = None,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run,
) -> MigrationResult:
    if client not in CLIENTS:
        raise ValueError("migration client must be codex or claude-code")
    root = (home or Path.home()).resolve()
    mcp_state = _inspect_mcp(client, root, cwd)
    if mcp_state == "unrecognized":
        raise RuntimeError(
            f"{client} has an unknown/custom readndraft MCP entry; refusing to remove it"
        )

    skill_states = {
        name: _skill_state(client, name, paths=paths, home=root)
        for name in LEGACY_SKILL_NAMES
    }
    blocked = [
        name
        for name, state in skill_states.items()
        if state in {"modified", "unmanaged"}
    ]
    if blocked:
        raise RuntimeError(
            "modified or unmanaged legacy skills were preserved: " + ", ".join(blocked)
        )

    removed_mcp = False
    if mcp_state == "recognized":
        command = ["codex", "mcp", "remove", "readndraft"]
        if client == "claude-code":
            command = ["claude", "mcp", "remove", "readndraft", "--scope", "user"]
        completed = runner(command)
        if completed.returncode:
            raise RuntimeError(
                f"{client} could not remove the recognized legacy MCP entry"
            )
        removed_mcp = True

    removed_skills: list[str] = []
    for name, state in skill_states.items():
        if state == "managed":
            _remove_managed_skill(client, name, paths=paths, home=root)
            removed_skills.append(name)
    return MigrationResult(client, removed_mcp, tuple(removed_skills))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remove a recognized legacy readNdraft MCP/skill installation before using the marketplace plugin"
    )
    parser.add_argument("--client", required=True, choices=CLIENTS)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    if not args.yes:
        if not sys.stdin.isatty():
            print("Migration requires --yes when input is not interactive.", file=sys.stderr)
            return 1
        answer = input(
            f"Remove only recognized legacy readNdraft integration files for {args.client}? (y/N): "
        )
        if answer.strip().casefold() not in {"y", "yes"}:
            print("Migration cancelled.")
            return 1
    try:
        result = migrate_plugin(args.client, paths=current_app_paths())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1
    print(f"Legacy MCP removed: {'yes' if result.mcp_removed else 'not present'}")
    print("Legacy skills removed: " + (", ".join(result.skills_removed) or "none"))
    print(
        "Install/enable readndraft@readndraft with the client plugin manager, then start a new session."
    )
    print("Account, keyring, audit, attachment, and draft data were not changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
