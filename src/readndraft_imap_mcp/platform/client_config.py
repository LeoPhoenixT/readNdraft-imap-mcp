from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import sys
from pathlib import Path


def resolve_command(
    value: str | None = None, *, executable: str = "readndraft-mcp"
) -> str:
    candidate = value or shutil.which(executable)
    if not candidate:
        raise RuntimeError(f"{executable} is not installed on PATH")
    path = Path(candidate).expanduser().resolve()
    if not path.is_absolute() or not path.is_file():
        raise ValueError("MCP command must be an existing absolute file")
    return str(path)


def codex_config(
    command: str,
    args: list[str] | tuple[str, ...] = (),
    *,
    startup_timeout: int = 10,
) -> str:
    lines = [
            "[mcp_servers.readndraft]",
            f"command = {json.dumps(command)}",
    ]
    if args:
        lines.append(f"args = {json.dumps(list(args))}")
    lines.extend(
        (
            f"startup_timeout_sec = {startup_timeout}",
            "tool_timeout_sec = 60",
            "required = true",
            'default_tools_approval_mode = "approve"',
            "",
        )
    )
    return "\n".join(lines)


def claude_code_config(
    command: str, args: list[str] | tuple[str, ...] = ()
) -> str:
    return json.dumps(
        {"type": "stdio", "command": command, "args": list(args), "env": {}},
        indent=2,
        sort_keys=True,
    ) + "\n"


def uvx_invocation(version: str | None = None) -> tuple[str, list[str]]:
    installed = version or importlib.metadata.version("readndraft-imap-mcp")
    if sys.platform == "win32":
        # uvw is uv's official GUI-subsystem alias. It launches uv with
        # CREATE_NO_WINDOW while preserving the MCP client's stdio handles.
        command = resolve_command(executable="uvw")
        return command, ["tool", "run", f"readndraft-imap-mcp@{installed}", "mcp"]
    command = resolve_command(executable="uvx")
    return command, [f"readndraft-imap-mcp@{installed}", "mcp"]


def client_config(client: str, *, uvx: bool, on_demand: bool = False) -> str:
    if uvx:
        command, args = uvx_invocation()
    else:
        executable = "readndraft-launch" if on_demand else "readndraft-mcp"
        command, args = resolve_command(executable=executable), []
    if client == "claude-code":
        return claude_code_config(command, args)
    return codex_config(command, args, startup_timeout=30 if uvx else 10)


def main(argv: list[str] | None = None, *, default_uvx: bool = False) -> int:
    parser = argparse.ArgumentParser(
        description="Print a secret-free readNdraft MCP client configuration"
    )
    parser.add_argument(
        "client", choices=("codex", "chatgpt-desktop", "claude-code")
    )
    parser.add_argument(
        "--command", help="Existing absolute MCP or launcher executable path"
    )
    parser.add_argument(
        "--on-demand",
        action="store_true",
        help="Opt in to readndraft-launch automatic broker startup",
    )
    parser.add_argument(
        "--uvx",
        action="store_true",
        default=default_uvx,
        help="Run the pinned package through uvx",
    )
    args = parser.parse_args(argv)
    if args.uvx and args.command:
        parser.error("--command cannot be combined with --uvx")
    if args.uvx:
        command, command_args = uvx_invocation()
    else:
        executable = "readndraft-launch" if args.on_demand else "readndraft-mcp"
        command = resolve_command(args.command, executable=executable)
        command_args = []
    if args.client == "claude-code":
        print(claude_code_config(command, command_args), end="")
    else:
        print(
            codex_config(
                command,
                command_args,
                startup_timeout=30 if args.uvx else 10,
            ),
            end="",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
