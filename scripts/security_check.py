from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
FORBIDDEN_IMPORTS = {"smtplib", "aiosmtplib", "yagmail", "sendgrid"}
FORBIDDEN_DEPENDENCIES = FORBIDDEN_IMPORTS | {"mailgun", "emails"}
SECRET_PATTERNS = (
    re.compile("AK" + r"IA[0-9A-Z]{16}"),
    re.compile("gh" + r"[pousr]_[A-Za-z0-9]{30,}"),
    re.compile("-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def _dependency_name(requirement: str) -> str:
    return re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip().casefold()


def check_dependencies() -> list[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"].get("dependencies", [])
    found = FORBIDDEN_DEPENDENCIES & {_dependency_name(item) for item in dependencies}
    return [f"forbidden dependency: {name}" for name in sorted(found)]


def check_imports() -> list[str]:
    errors: list[str] = []
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [item.name.partition(".")[0] for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.partition(".")[0]]
            for name in FORBIDDEN_IMPORTS & set(names):
                errors.append(f"forbidden import {name}: {path.relative_to(ROOT)}")
    return errors


def check_secrets() -> list[str]:
    errors: list[str] = []
    roots = [ROOT / "src", ROOT / "docs", ROOT / ".github", ROOT / "README.md"]
    for root in roots:
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file() or path.suffix.casefold() not in {
                ".md", ".py", ".toml", ".yaml", ".yml"
            }:
                continue
            text = path.read_text(encoding="utf-8")
            if any(pattern.search(text) for pattern in SECRET_PATTERNS):
                errors.append(f"possible committed secret: {path.relative_to(ROOT)}")
    return errors


def main() -> int:
    errors = check_dependencies() + check_imports() + check_secrets()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Security policy checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
