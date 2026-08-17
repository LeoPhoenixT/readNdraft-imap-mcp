from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from readndraft_imap_mcp import __version__

from .client_config import claude_code_config, codex_config, uvx_invocation
from .paths import AppPaths, current_app_paths
from .skill import SKILL_NAMES, install_skill, skill_destination, skill_status

CLIENTS = ("codex", "claude-code")
PACKAGE = "readndraft-imap-mcp"
_PIN = re.compile(r"^readndraft-imap-mcp@([^\s]+)$")
_TABLE = re.compile(r"(?m)^\s*\[([^\]\r\n]+)\]\s*(?:#.*)?$")


@dataclass(frozen=True, slots=True)
class ClientUpdateStatus:
    client: str
    target_version: str
    installed_version: str | None
    mcp: str
    skills: dict[str, str]
    restart_required: bool


def _pin_from_args(args: object) -> str | None:
    if not isinstance(args, list):
        return None
    matches = [match.group(1) for value in args if isinstance(value, str) if (match := _PIN.match(value))]
    return matches[0] if len(matches) == 1 and args[-1:] == ["mcp"] else None


def _recognized_invocation(command: object, args: object) -> str | None:
    if not isinstance(command, str) or not command:
        return None
    executable = Path(command).name.casefold()
    if executable not in {"uvx", "uvx.exe", "uvw", "uvw.exe"}:
        return None
    version = _pin_from_args(args)
    if version is None:
        return None
    expected = [f"{PACKAGE}@{version}", "mcp"]
    if executable in {"uvw", "uvw.exe"}:
        expected = ["tool", "run", *expected]
    return version if args == expected else None


def _codex_path(home: Path) -> Path:
    return home / ".codex" / "config.toml"


def _claude_path(home: Path) -> Path:
    return home / ".claude.json"


def _read_codex_server(home: Path) -> tuple[str, str | None]:
    path = _codex_path(home)
    if not path.is_file():
        return "not installed", None
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "unrecognized", None
    server = document.get("mcp_servers", {}).get("readndraft")
    if server is None:
        return "not installed", None
    if not isinstance(server, dict):
        return "unrecognized", None
    version = _recognized_invocation(server.get("command"), server.get("args"))
    return ("recognized", version) if version else ("unrecognized", None)


def _read_claude_server(home: Path) -> tuple[str, str | None]:
    path = _claude_path(home)
    if not path.is_file():
        return "not installed", None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unrecognized", None
    servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        return "unrecognized", None
    projects = document.get("projects", {})
    if not isinstance(projects, dict):
        return "unrecognized", None
    for project in projects.values():
        if not isinstance(project, dict):
            continue
        local_servers = project.get("mcpServers", {})
        if isinstance(local_servers, dict) and "readndraft" in local_servers:
            return "unrecognized", None
    server = servers.get("readndraft")
    if server is None:
        return "not installed", None
    if not isinstance(server, dict):
        return "unrecognized", None
    version = _recognized_invocation(server.get("command"), server.get("args"))
    return ("recognized", version) if version else ("unrecognized", None)


def _project_claude_entry_present(cwd: Path | None = None) -> bool:
    path = (cwd or Path.cwd()) / ".mcp.json"
    if not path.is_file():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    servers = document.get("mcpServers", {})
    return not isinstance(servers, dict) or "readndraft" in servers


def inspect_client(
    client: str,
    *,
    paths: AppPaths,
    home: Path | None = None,
    target_version: str = __version__,
) -> ClientUpdateStatus:
    if client not in CLIENTS:
        raise ValueError("update client must be codex or claude-code")
    root = (home or Path.home()).resolve()
    if client == "codex":
        state, installed = _read_codex_server(root)
    elif _project_claude_entry_present():
        state, installed = "unrecognized", None
    else:
        state, installed = _read_claude_server(root)
    if state == "recognized":
        mcp = "current" if installed == target_version else "outdated"
    else:
        mcp = state
    skills = {
        name: skill_status(client, paths=paths, home=root, skill_name=name)[0]
        for name in SKILL_NAMES
    }
    restart = mcp != "current" or any(value != "current" for value in skills.values())
    return ClientUpdateStatus(client, target_version, installed, mcp, skills, restart)


def _blocked_skills(status: ClientUpdateStatus) -> list[str]:
    return [
        name
        for name, state in status.skills.items()
        if state in {"modified", "unmanaged"}
    ]


def _validate_preflight(status: ClientUpdateStatus, force_skill: bool) -> None:
    if status.mcp == "unrecognized":
        raise RuntimeError(
            f"{status.client} MCP entry is unrecognized; refusing to overwrite"
        )
    blocked = _blocked_skills(status)
    if blocked and not force_skill:
        raise RuntimeError(
            "modified or unmanaged skills require --force-skill: "
            + ", ".join(blocked)
        )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else None
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    if mode is not None:
        os.chmod(temporary, mode)
    elif os.name != "nt":
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _replace_codex_table(text: str, replacement: str) -> str:
    matches = list(_TABLE.finditer(text))
    selected = [item for item in matches if item.group(1).strip() == "mcp_servers.readndraft"]
    if len(selected) > 1:
        raise RuntimeError("duplicate Codex readndraft MCP tables")
    if not selected:
        separator = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return text + separator + replacement
    start = selected[0].start()
    end = len(text)
    selected_index = matches.index(selected[0])
    for following in matches[selected_index + 1 :]:
        name = following.group(1).strip()
        if not name.startswith("mcp_servers.readndraft."):
            end = following.start()
            break
    return text[:start] + replacement + text[end:]


def _backup_file(path: Path, paths: AppPaths, client: str) -> Path | None:
    if not path.is_file():
        return None
    directory = paths.state_dir / "update-backups"
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(directory, 0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"{stamp}-{client}-{path.name}"
    shutil.copy2(path, target)
    if os.name != "nt":
        os.chmod(target, 0o600)
    return target


def _restore_file(path: Path, snapshot: Path | None, existed: bool) -> None:
    if existed and snapshot is not None:
        shutil.copy2(snapshot, path)
    elif not existed:
        path.unlink(missing_ok=True)


def _snapshot_skills(client: str, home: Path, transaction: Path) -> dict[str, str]:
    present: dict[str, str] = {}
    for name in SKILL_NAMES:
        target = skill_destination(client, home, name)
        if target.is_dir():
            present[name] = "directory"
            shutil.copytree(target, transaction / name)
        elif target.exists():
            present[name] = "file"
            shutil.copy2(target, transaction / name)
        else:
            present[name] = "missing"
    return present


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _restore_skills(client: str, home: Path, transaction: Path, present: dict[str, str]) -> None:
    for name in SKILL_NAMES:
        target = skill_destination(client, home, name)
        _remove_path(target)
        if present[name] == "directory":
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(transaction / name, target)
        elif present[name] == "file":
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(transaction / name, target)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _apply_codex(home: Path, version: str) -> None:
    path = _codex_path(home)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    command, args = uvx_invocation(version)
    replacement = codex_config(command, args, startup_timeout=30)
    _atomic_write(path, _replace_codex_table(text, replacement))


def _apply_claude(
    home: Path,
    version: str,
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> None:
    state, _ = _read_claude_server(home)
    if state == "recognized":
        removed = runner(["claude", "mcp", "remove", "readndraft", "--scope", "user"])
        if removed.returncode:
            raise RuntimeError("Claude Code could not remove the existing user-scoped entry")
    command, args = uvx_invocation(version)
    configuration = claude_code_config(command, args).strip()
    added = runner(
        [
            "claude",
            "mcp",
            "add-json",
            "readndraft",
            configuration,
            "--scope",
            "user",
        ]
    )
    if added.returncode:
        raise RuntimeError("Claude Code could not add the updated user-scoped entry")


def apply_update(
    client: str,
    *,
    paths: AppPaths,
    home: Path | None = None,
    target_version: str = __version__,
    force_skill: bool = False,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run,
) -> ClientUpdateStatus:
    root = (home or Path.home()).resolve()
    before = inspect_client(client, paths=paths, home=root, target_version=target_version)
    _validate_preflight(before, force_skill)
    if (
        before.mcp == "current"
        and all(value == "current" for value in before.skills.values())
        and not force_skill
    ):
        return before

    paths.ensure_private()
    config_path = _codex_path(root) if client == "codex" else _claude_path(root)
    config_existed = config_path.is_file()
    backup = _backup_file(config_path, paths, client)
    manifest = paths.state_dir / "skill-installs.json"
    manifest_existed = manifest.is_file()
    manifest_backup = _backup_file(manifest, paths, f"{client}-skills")
    transaction = Path(tempfile.mkdtemp(prefix=f"update-{client}-", dir=paths.state_dir))
    skill_present = _snapshot_skills(client, root, transaction)
    try:
        if client == "codex":
            _apply_codex(root, target_version)
        else:
            _apply_claude(root, target_version, runner=runner)
        for name in SKILL_NAMES:
            install_skill(
                client,
                paths=paths,
                home=root,
                force=force_skill,
                skill_name=name,
            )
        after = inspect_client(client, paths=paths, home=root, target_version=target_version)
        if after.mcp != "current" or any(value != "current" for value in after.skills.values()):
            raise RuntimeError("post-update verification failed")
        return ClientUpdateStatus(
            after.client,
            after.target_version,
            after.installed_version,
            after.mcp,
            after.skills,
            True,
        )
    except Exception:
        _restore_file(config_path, backup, config_existed)
        _restore_file(manifest, manifest_backup, manifest_existed)
        _restore_skills(client, root, transaction, skill_present)
        raise
    finally:
        shutil.rmtree(transaction, ignore_errors=True)


def _selected_clients(value: str | None, all_clients: bool) -> tuple[str, ...]:
    if all_clients:
        return CLIENTS
    if value is None:
        raise ValueError("choose --client or --all")
    return (value,)


def _print_status(items: list[ClientUpdateStatus], as_json: bool) -> None:
    if as_json:
        print(json.dumps([asdict(item) for item in items], indent=2, sort_keys=True))
        return
    for item in items:
        installed = item.installed_version or "none"
        print(f"{item.client}: MCP {item.mcp} (installed {installed}, target {item.target_version})")
        for name, state in item.skills.items():
            print(f"  {name}: {state}")
        print(f"  restart required: {'yes' if item.restart_required else 'no'}")


def main(argv: list[str] | None = None) -> int:
    print(
        "Warning: readndraft update is deprecated; use the client-native plugin update lifecycle after migration.",
        file=sys.stderr,
    )
    parser = argparse.ArgumentParser(description="Check or apply readNdraft client updates")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("check", "apply"):
        item = commands.add_parser(action)
        item.add_argument("--client", choices=CLIENTS)
        item.add_argument("--all", action="store_true")
        item.add_argument("--json", action="store_true")
        if action == "apply":
            item.add_argument("--yes", action="store_true")
            item.add_argument("--force-skill", action="store_true")
    args = parser.parse_args(argv)
    if args.all and args.client:
        parser.error("--client and --all are mutually exclusive")
    try:
        clients = _selected_clients(args.client, args.all)
        paths = current_app_paths()
        if args.action == "check":
            _print_status([inspect_client(client, paths=paths) for client in clients], args.json)
            return 0
        previews = [inspect_client(client, paths=paths) for client in clients]
        for preview in previews:
            _validate_preflight(preview, args.force_skill)
        if not args.yes:
            if not sys.stdin.isatty():
                raise RuntimeError("update apply requires --yes when input is not interactive")
            _print_status(previews, False)
            summary = ", ".join(
                f"{item.client} to {item.target_version}" for item in previews
            )
            if input(f"Update readNdraft MCP and both managed skills for {summary}? (y/N): ").strip().casefold() not in {"y", "yes"}:
                print("Update cancelled.")
                return 1
            forced = {
                item.client: _blocked_skills(item)
                for item in previews
                if _blocked_skills(item)
            }
            if forced and input(
                "Replace the listed modified or unmanaged skill directories? (y/N): "
            ).strip().casefold() not in {"y", "yes"}:
                print("Forced skill replacement cancelled.")
                return 1
        results: list[ClientUpdateStatus] = []
        for client in clients:
            try:
                results.append(
                    apply_update(
                        client, paths=paths, force_skill=args.force_skill
                    )
                )
            except Exception as exc:
                completed = ", ".join(item.client for item in results)
                suffix = f"; already completed: {completed}" if completed else ""
                raise RuntimeError(f"{client} update failed{suffix}: {exc}") from exc
        _print_status(results, args.json)
        print("Retire a surviving broker with: readndraft-imap-mcp broker stop")
        print("Fully restart each updated client before using readNdraft again.")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
