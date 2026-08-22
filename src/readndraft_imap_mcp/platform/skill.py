from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sys
from pathlib import Path

from ._utils import remove_path as _remove_path
from .paths import AppPaths, current_app_paths

DEFAULT_SKILL_NAME = "readndraft-email"
SKILL_NAMES = (DEFAULT_SKILL_NAME, "readndraft-update")


def _validate_skill_name(skill_name: str) -> str:
    if skill_name not in SKILL_NAMES:
        raise ValueError(f"skill must be one of: {', '.join(SKILL_NAMES)}")
    return skill_name


def bundled_skill_dir(skill_name: str = DEFAULT_SKILL_NAME) -> Path:
    skill_name = _validate_skill_name(skill_name)
    packaged = Path(__file__).resolve().parents[1] / "_skills" / skill_name
    if packaged.is_dir():
        return packaged
    repository_root = Path(__file__).resolve().parents[3]
    repository = (
        repository_root / "plugins" / "readndraft" / "skills" / skill_name
        if skill_name == DEFAULT_SKILL_NAME
        else repository_root / "skills" / skill_name
    )
    if repository.is_dir():
        return repository
    raise RuntimeError("packaged readNdraft skill is unavailable")


def skill_destination(
    client: str,
    home: Path | None = None,
    skill_name: str = DEFAULT_SKILL_NAME,
) -> Path:
    skill_name = _validate_skill_name(skill_name)
    root = (home or Path.home()).resolve()
    if client == "codex":
        return root / ".agents" / "skills" / skill_name
    if client == "claude-code":
        return root / ".claude" / "skills" / skill_name
    raise ValueError("skill client must be codex or claude-code")


def _digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _manifest_path(paths: AppPaths) -> Path:
    return paths.state_dir / "skill-installs.json"


def _manifest_key(client: str, skill_name: str) -> str:
    return f"{client}:{skill_name}"


def _manifest_record(
    manifest: dict[str, dict[str, str]], client: str, skill_name: str
) -> dict[str, str] | None:
    record = manifest.get(_manifest_key(client, skill_name))
    if record is None and skill_name == DEFAULT_SKILL_NAME:
        # Read manifests written by releases that managed only readndraft-email.
        record = manifest.get(client)
    return record


def _load_manifest(paths: AppPaths) -> dict[str, dict[str, str]]:
    try:
        value = json.loads(_manifest_path(paths).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError("invalid skill installation manifest")
    return value


def _save_manifest(paths: AppPaths, value: dict[str, dict[str, str]]) -> None:
    paths.ensure_private()
    target = _manifest_path(paths)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(temporary, 0o600)
    os.replace(temporary, target)


def install_skill(
    client: str,
    *,
    paths: AppPaths,
    home: Path | None = None,
    force: bool = False,
    skill_name: str = DEFAULT_SKILL_NAME,
) -> Path:
    skill_name = _validate_skill_name(skill_name)
    source = bundled_skill_dir(skill_name)
    target = skill_destination(client, home, skill_name)
    manifest = _load_manifest(paths)
    current = _manifest_record(manifest, client, skill_name)
    key = _manifest_key(client, skill_name)
    if target.exists():
        actual = _digest(target)
        if actual == _digest(source):
            manifest[key] = {"path": str(target), "hash": actual}
            if skill_name == DEFAULT_SKILL_NAME:
                manifest.pop(client, None)
            _save_manifest(paths, manifest)
            return target
        managed = current and current.get("path") == str(target) and current.get("hash") == actual
        if not force and not managed:
            raise FileExistsError("existing skill is unrecognized or modified; refusing to overwrite")
        token = secrets.token_hex(8)
        backup = target.with_name(f".{target.name}.{token}.previous")
        os.replace(target, backup)
    else:
        backup = None
    target.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(8)
    temporary = target.with_name(f".{target.name}.{token}.installing")
    try:
        shutil.copytree(source, temporary)
        os.replace(temporary, target)
        manifest[key] = {"path": str(target), "hash": _digest(target)}
        if skill_name == DEFAULT_SKILL_NAME:
            manifest.pop(client, None)
        _save_manifest(paths, manifest)
    except Exception:
        _remove_path(temporary)
        _remove_path(target)
        if backup is not None and backup.exists():
            os.replace(backup, target)
        raise
    if backup is not None:
        _remove_path(backup)
    return target


def install_all_skills(
    client: str,
    *,
    paths: AppPaths,
    home: Path | None = None,
    force: bool = False,
) -> tuple[Path, ...]:
    return tuple(
        install_skill(
            client, paths=paths, home=home, force=force, skill_name=skill_name
        )
        for skill_name in SKILL_NAMES
    )


def skill_status(
    client: str,
    *,
    paths: AppPaths,
    home: Path | None = None,
    skill_name: str = DEFAULT_SKILL_NAME,
) -> tuple[str, Path]:
    """Report whether an installed skill is current, outdated, or unmanaged."""

    skill_name = _validate_skill_name(skill_name)
    source = bundled_skill_dir(skill_name)
    target = skill_destination(client, home, skill_name)
    if not target.exists():
        return "not installed", target
    if not target.is_dir():
        return "unmanaged", target
    actual = _digest(target)
    if actual == _digest(source):
        return "current", target
    current = _manifest_record(_load_manifest(paths), client, skill_name)
    if current and current.get("path") == str(target):
        if current.get("hash") == actual:
            return "outdated", target
        return "modified", target
    return "unmanaged", target


def uninstall_skill(
    client: str,
    *,
    paths: AppPaths,
    home: Path | None = None,
    skill_name: str = DEFAULT_SKILL_NAME,
) -> None:
    skill_name = _validate_skill_name(skill_name)
    target = skill_destination(client, home, skill_name)
    manifest = _load_manifest(paths)
    current = _manifest_record(manifest, client, skill_name)
    if not current or current.get("path") != str(target) or not target.is_dir():
        raise FileNotFoundError("no managed readNdraft skill installation found")
    if _digest(target) != current.get("hash"):
        raise RuntimeError("installed skill was modified; refusing to remove it")
    shutil.rmtree(target)
    manifest.pop(_manifest_key(client, skill_name), None)
    if skill_name == DEFAULT_SKILL_NAME:
        manifest.pop(client, None)
    _save_manifest(paths, manifest)


def main(argv: list[str] | None = None) -> int:
    print(
        "Warning: direct skill installation is deprecated; Claude Code and Codex users "
        "should install the readNdraft plugin.",
        file=sys.stderr,
    )
    parser = argparse.ArgumentParser(description="Manage the packaged readNdraft Agent Skill")
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("install", "uninstall", "status"):
        item = commands.add_parser(name)
        item.add_argument("values", nargs="+")
        item.add_argument("--all", action="store_true")
        if name == "install":
            item.add_argument("--force", action="store_true")
    print_parser = commands.add_parser("print")
    print_parser.add_argument("skill_name", nargs="?", default=DEFAULT_SKILL_NAME)
    args = parser.parse_args(argv)
    paths = current_app_paths()
    try:
        if args.action == "print":
            print((bundled_skill_dir(args.skill_name) / "SKILL.md").read_text(encoding="utf-8"), end="")
        else:
            if args.all:
                if len(args.values) != 1:
                    raise ValueError("--all requires exactly one client")
                client, skill_names = args.values[0], SKILL_NAMES
            elif len(args.values) == 1:
                client, skill_names = args.values[0], (DEFAULT_SKILL_NAME,)
            elif len(args.values) == 2:
                skill_names, client = (args.values[0],), args.values[1]
            else:
                raise ValueError("expected CLIENT or SKILL CLIENT")
            if client not in {"codex", "claude-code"}:
                raise ValueError("skill client must be codex or claude-code")
            for skill_name in skill_names:
                if args.action == "install":
                    target = install_skill(
                        client,
                        paths=paths,
                        force=args.force,
                        skill_name=skill_name,
                    )
                    print(f"Installed {skill_name} at {target}")
                elif args.action == "uninstall":
                    uninstall_skill(
                        client, paths=paths, skill_name=skill_name
                    )
                    print(f"Uninstalled {skill_name} for {client}")
                else:
                    state, target = skill_status(
                        client, paths=paths, skill_name=skill_name
                    )
                    print(f"{client} {skill_name}: {state} ({target})")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Skill operation failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
