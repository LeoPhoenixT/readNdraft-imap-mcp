from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ._utils import run as _run
from .paths import AppPaths, current_app_paths
from .skill import (
    DEFAULT_SKILL_NAME,
    SKILL_NAMES,
    _digest,
    _load_manifest,
    _manifest_key,
    _manifest_path,
    _manifest_record,
    _save_manifest,
    bundled_skill_dir,
    skill_destination,
    skill_status,
)
from .update import (
    CLIENTS,
    _project_claude_entry_present,
    _read_claude_server,
    _read_codex_server,
)


@dataclass(frozen=True, slots=True)
class MigrationResult:
    client: str
    mcp_removed: bool
    skills_removed: tuple[str, ...]


def _inspect_mcp(client: str, home: Path, cwd: Path | None) -> str:
    if client == "codex":
        return _read_codex_server(home)[0]
    if _project_claude_entry_present(cwd):
        return "unrecognized"
    return _read_claude_server(home)[0]


def _remove_clean_skill(client: str, name: str, *, paths: AppPaths, home: Path) -> None:
    target = skill_destination(client, home, name)
    manifest = _load_manifest(paths)
    record = _manifest_record(manifest, client, name)
    source_matches = target.is_dir() and _digest(target) == _digest(bundled_skill_dir(name))
    manifest_matches = (
        target.is_dir()
        and record is not None
        and record.get("path") == str(target)
        and record.get("hash") == _digest(target)
    )
    if not (source_matches or manifest_matches):
        raise RuntimeError(f"{name} is not a confirmed unmodified readNdraft skill")
    import shutil

    shutil.rmtree(target)
    manifest.pop(_manifest_key(client, name), None)
    if name == DEFAULT_SKILL_NAME:
        manifest.pop(client, None)
    if manifest:
        _save_manifest(paths, manifest)
    else:
        _manifest_path(paths).unlink(missing_ok=True)


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
        raise RuntimeError(f"{client} has an unknown/custom readndraft MCP entry; refusing to remove it")

    skill_states = {
        name: skill_status(client, paths=paths, home=root, skill_name=name)[0]
        for name in SKILL_NAMES
    }
    blocked = [name for name, state in skill_states.items() if state in {"modified", "unmanaged"}]
    if blocked:
        raise RuntimeError("modified or unmanaged legacy skills were preserved: " + ", ".join(blocked))

    removed_mcp = False
    if mcp_state == "recognized":
        command = ["codex", "mcp", "remove", "readndraft"]
        if client == "claude-code":
            command = ["claude", "mcp", "remove", "readndraft", "--scope", "user"]
        completed = runner(command)
        if completed.returncode:
            raise RuntimeError(f"{client} could not remove the recognized legacy MCP entry")
        removed_mcp = True

    removed_skills: list[str] = []
    for name, state in skill_states.items():
        if state in {"current", "outdated"}:
            _remove_clean_skill(client, name, paths=paths, home=root)
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
    print("Install/enable readndraft@readndraft with the client plugin manager, then start a new session.")
    print("Account, keyring, audit, attachment, and draft data were not changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
