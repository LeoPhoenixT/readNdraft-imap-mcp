from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Iterable

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from readndraft_imap_mcp.ipc.contract import canonical_tool_schema_digest

SERVER_NAME = "readndraft_dev"
SUCCESS_MARKER = "READNDRAFT_DEV_MCP_OK"
EXPECTED_TOOLS = frozenset(
    {
        "list_accounts",
        "list_mailboxes",
        "search_emails",
        "get_email",
        "get_emails",
        "get_email_html",
        "list_attachment_inputs",
        "save_attachment",
        "create_draft",
        "move_email",
        "update_draft",
        "set_star",
        "set_read_state",
        "set_read_state_batch",
        "set_star_batch",
        "move_emails_batch",
    }
)
EXPECTED_TOOL_SCHEMA_DIGEST = "337e10a8714f548a320e49078dc9f0714921653c37ac61a3b6af83ef264349f8"


def _event_items(events: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for event in events:
        item = event.get("item")
        if isinstance(item, dict):
            yield item


def _final_message(items: Iterable[dict[str, Any]]) -> str:
    messages = [
        item.get("text", "")
        for item in items
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str)
    ]
    return messages[-1] if messages else ""


def _parse_events(output: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("Codex JSONL event must be an object")
        events.append(value)
    return events


def _run(command: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _required_mcp_overrides(root: Path) -> list[str]:
    values = {
        "command": "uv",
        "args": [
            "run",
            "--locked",
            "--no-sync",
            "readndraft-imap-mcp",
            "mcp",
        ],
        "cwd": str(root),
        "startup_timeout_sec": 10,
        "tool_timeout_sec": 60,
        "required": True,
        "default_tools_approval_mode": "approve",
    }
    result: list[str] = []
    for key, value in values.items():
        result.extend(["-c", f"mcp_servers.{SERVER_NAME}.{key}={json.dumps(value)}"])
    return result


async def _list_local_tools(root: Path) -> dict[str, dict[str, Any]]:
    parameters = StdioServerParameters(
        command="uv",
        args=["run", "--locked", "--no-sync", "readndraft-imap-mcp", "mcp"],
        cwd=root,
    )
    async with AsyncExitStack() as stack:
        errlog = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
        read_stream, write_stream = await stack.enter_async_context(stdio_client(parameters, errlog=errlog))
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        listed = await session.list_tools()
        return {
            tool.name: {
                "inputSchema": tool.inputSchema,
                "outputSchema": tool.outputSchema,
            }
            for tool in listed.tools
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the repo-local readndraft_dev MCP through a fresh Codex session."
    )
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    codex = shutil.which("codex")
    if not codex:
        print("Codex CLI is not installed on PATH.", file=sys.stderr)
        return 2

    configured = _run(
        [codex, "-C", str(root), "mcp", "get", SERVER_NAME, "--json"],
        cwd=root,
        timeout=30,
    )
    if configured.returncode != 0:
        print(
            "Codex did not load the repo-local readndraft_dev MCP configuration.\n"
            "Trust this repository and start a new Codex session before retrying.",
            file=sys.stderr,
        )
        return 1

    try:
        tools = asyncio.run(_list_local_tools(root))
    except Exception as exc:
        print(
            f"Local readndraft_dev MCP initialization failed: {type(exc).__name__}.",
            file=sys.stderr,
        )
        return 1
    if set(tools) != EXPECTED_TOOLS:
        print(
            "Local readndraft_dev MCP tool catalog differs from the exact expected set.",
            file=sys.stderr,
        )
        return 1
    if any(not schema["inputSchema"] or not schema["outputSchema"] for schema in tools.values()):
        print("Local readndraft_dev MCP returned an incomplete tool schema.", file=sys.stderr)
        return 1
    if canonical_tool_schema_digest(tools) != EXPECTED_TOOL_SCHEMA_DIGEST:
        print("Local readndraft_dev MCP tool schemas differ from the pinned contract.", file=sys.stderr)
        return 1

    prompt = f"Do not call any tool and do not inspect files. Reply with exactly {SUCCESS_MARKER}."
    completed = _run(
        [
            codex,
            "-a",
            "never",
            *_required_mcp_overrides(root),
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--json",
            "--sandbox",
            "read-only",
            "-C",
            str(root),
            prompt,
        ],
        cwd=root,
        timeout=args.timeout,
    )
    if completed.returncode != 0:
        print(
            f"Fresh Codex session failed with exit code {completed.returncode}.",
            file=sys.stderr,
        )
        return 1

    try:
        events = _parse_events(completed.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"Codex returned invalid JSONL: {exc}", file=sys.stderr)
        return 1

    items = list(_event_items(events))
    if _final_message(items).strip() != SUCCESS_MARKER:
        print("Fresh Codex session returned an unexpected final marker.", file=sys.stderr)
        return 1

    print("Codex development MCP smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
