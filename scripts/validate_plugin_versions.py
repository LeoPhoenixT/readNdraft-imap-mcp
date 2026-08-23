from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "readndraft-imap-mcp"


def versions(root: Path = ROOT) -> dict[str, str]:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    codex = json.loads(
        (root / "plugins/readndraft/.codex-plugin/plugin.json").read_text(encoding="utf-8")
    )
    claude = json.loads(
        (root / "plugins/readndraft/.claude-plugin/plugin.json").read_text(encoding="utf-8")
    )
    marketplace = json.loads(
        (root / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
    )
    mcp = json.loads((root / "plugins/readndraft/.mcp.json").read_text(encoding="utf-8"))
    args = mcp["mcpServers"]["readndraft"]["args"]
    pins = [
        match.group(1)
        for value in args
        if (match := re.fullmatch(rf"{re.escape(PACKAGE)}@([^\s]+)", value))
    ]
    if len(pins) != 1:
        raise ValueError("plugin MCP command must contain exactly one package version pin")
    return {
        "project": project["project"]["version"],
        "codex_plugin": codex["version"],
        "claude_plugin": claude["version"],
        "claude_marketplace": marketplace["plugins"][0]["version"],
        "mcp_pin": pins[0],
    }


def main() -> int:
    found = versions()
    if len(set(found.values())) != 1:
        print("Plugin version mismatch: " + ", ".join(f"{key}={value}" for key, value in found.items()))
        return 1
    print(f"Plugin versions agree: {next(iter(found.values()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
