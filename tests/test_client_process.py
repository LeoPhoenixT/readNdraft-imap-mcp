from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time

import pytest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from readndraft_imap_mcp.platform.launcher import broker_healthy
from readndraft_imap_mcp.platform.paths import current_app_paths
from readndraft_imap_mcp.poc.ipc import run_ipc_probe


async def _exercise_stdio(tmp_path) -> None:
    environment = os.environ.copy()
    if sys.platform == "win32":
        environment["LOCALAPPDATA"] = str((tmp_path / "local-app-data").resolve())
    else:
        environment["XDG_CONFIG_HOME"] = str((tmp_path / "config").resolve())
        environment["XDG_STATE_HOME"] = str((tmp_path / "state").resolve())
        environment["XDG_RUNTIME_DIR"] = str((tmp_path / "runtime").resolve())
        environment["XDG_DATA_HOME"] = str((tmp_path / "data").resolve())
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "readndraft_imap_mcp.mcp_server.server"],
        env=environment,
    )

    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()

    assert {tool.name for tool in listed.tools} == {
        "list_accounts",
        "list_mailboxes",
        "search_emails",
            "get_email",
            "get_emails",
        "get_email_html",
        "list_attachment_inputs",
        "save_attachment",
        "create_draft",
        "update_draft",
        "set_star",
            "set_read_state",
            "set_star_batch",
            "set_read_state_batch",
    }


def test_installed_stdio_frontend_initializes_for_real_client(tmp_path) -> None:
    asyncio.run(_exercise_stdio(tmp_path))


async def _exercise_on_demand_stdio(tmp_path, environment) -> None:
    launcher = shutil.which("readndraft-launch")
    assert launcher is not None
    parameters = StdioServerParameters(
        command=launcher,
        args=[
            "--startup-timeout",
            "10",
            "--idle-timeout",
            "0.4",
            "--shutdown-grace",
            "0.2",
        ],
        env=environment,
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            await asyncio.sleep(0.9)
            accounts = await session.call_tool("list_accounts")
            assert accounts.isError is False
            assert accounts.structuredContent == {"result": []}


def test_on_demand_launcher_starts_frontend_and_idle_broker(tmp_path, monkeypatch) -> None:
    try:
        run_ipc_probe()
    except RuntimeError as exc:
        if isinstance(exc.__cause__, PermissionError):
            pytest.skip("test sandbox prohibits local sockets")
        raise
    environment = os.environ.copy()
    if sys.platform == "win32":
        root = str((tmp_path / "local-app-data").resolve())
        environment["LOCALAPPDATA"] = root
        monkeypatch.setenv("LOCALAPPDATA", root)
    else:
        values = {
            "XDG_CONFIG_HOME": str((tmp_path / "config").resolve()),
            "XDG_STATE_HOME": str((tmp_path / "state").resolve()),
            "XDG_RUNTIME_DIR": str((tmp_path / "runtime").resolve()),
            "XDG_DATA_HOME": str((tmp_path / "data").resolve()),
        }
        environment.update(values)
        for name, value in values.items():
            monkeypatch.setenv(name, value)

    asyncio.run(_exercise_on_demand_stdio(tmp_path, environment))
    paths = current_app_paths()
    time.sleep(1)
    assert broker_healthy(paths, timeout=0.2) is False
