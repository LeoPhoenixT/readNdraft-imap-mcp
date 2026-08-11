from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

from .paths import AppPaths, current_app_paths

SKILL_NAME = "readndraft-email"


def bundled_skill_dir() -> Path:
    packaged = Path(__file__).resolve().parents[1] / "_skills" / SKILL_NAME
    if packaged.is_dir():
        return packaged
    repository = Path(__file__).resolve().parents[3] / "skills" / SKILL_NAME
    if repository.is_dir():
        return repository
    raise RuntimeError("packaged readNdraft skill is unavailable")


def skill_destination(client: str, home: Path | None = None) -> Path:
    root = (home or Path.home()).resolve()
    if client == "codex":
        return root / ".agents" / "skills" / SKILL_NAME
    if client == "claude-code":
        return root / ".claude" / "skills" / SKILL_NAME
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
) -> Path:
    source = bundled_skill_dir()
    target = skill_destination(client, home)
    manifest = _load_manifest(paths)
    current = manifest.get(client)
    if target.exists():
        actual = _digest(target)
        if actual == _digest(source):
            manifest[client] = {"path": str(target), "hash": actual}
            _save_manifest(paths, manifest)
            return target
        managed = current and current.get("path") == str(target) and current.get("hash") == actual
        if not force and not managed:
            raise FileExistsError("existing skill is unrecognized or modified; refusing to overwrite")
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    manifest[client] = {"path": str(target), "hash": _digest(target)}
    _save_manifest(paths, manifest)
    return target


def skill_status(
    client: str,
    *,
    paths: AppPaths,
    home: Path | None = None,
) -> tuple[str, Path]:
    """Report whether an installed skill is current, outdated, or unmanaged."""

    source = bundled_skill_dir()
    target = skill_destination(client, home)
    if not target.is_dir():
        return "not installed", target
    actual = _digest(target)
    if actual == _digest(source):
        return "current", target
    current = _load_manifest(paths).get(client)
    if current and current.get("path") == str(target):
        if current.get("hash") == actual:
            return "outdated", target
        return "modified", target
    return "unmanaged", target


def uninstall_skill(client: str, *, paths: AppPaths, home: Path | None = None) -> None:
    target = skill_destination(client, home)
    manifest = _load_manifest(paths)
    current = manifest.get(client)
    if not current or current.get("path") != str(target) or not target.is_dir():
        raise FileNotFoundError("no managed readNdraft skill installation found")
    if _digest(target) != current.get("hash"):
        raise RuntimeError("installed skill was modified; refusing to remove it")
    shutil.rmtree(target)
    del manifest[client]
    _save_manifest(paths, manifest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the packaged readNdraft Agent Skill")
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("install", "uninstall", "status"):
        item = commands.add_parser(name)
        item.add_argument("client", choices=("codex", "claude-code"))
        if name == "install":
            item.add_argument("--force", action="store_true")
    commands.add_parser("print")
    args = parser.parse_args(argv)
    paths = current_app_paths()
    try:
        if args.action == "print":
            print((bundled_skill_dir() / "SKILL.md").read_text(encoding="utf-8"), end="")
        elif args.action == "install":
            target = install_skill(args.client, paths=paths, force=args.force)
            print(f"Installed {SKILL_NAME} at {target}")
        elif args.action == "uninstall":
            uninstall_skill(args.client, paths=paths)
            print(f"Uninstalled {SKILL_NAME} for {args.client}")
        else:
            state, target = skill_status(args.client, paths=paths)
            print(f"{args.client}: {state} ({target})")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Skill operation failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
