from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def errors_for(tag: str) -> list[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    errors: list[str] = []
    if tag != f"v{project['version']}":
        errors.append("release tag must exactly match project version")
    if project.get("license") != "Apache-2.0":
        errors.append("project license must be the approved Apache-2.0 expression")
    if project.get("license-files") != ["LICENSE", "THIRD_PARTY_NOTICES.md"]:
        errors.append("project metadata must include license and third-party notices")
    if not (ROOT / "LICENSE").is_file():
        errors.append("root LICENSE file is missing")
    if not (ROOT / "THIRD_PARTY_NOTICES.md").is_file():
        errors.append("third-party dependency notices are missing")
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?", tag):
        errors.append("release tag is not a supported PEP 440 version tag")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed release metadata checks")
    parser.add_argument("--tag", required=True)
    args = parser.parse_args(argv)
    errors = errors_for(args.tag)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Release metadata checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
