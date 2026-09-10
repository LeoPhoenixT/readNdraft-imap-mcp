from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from readndraft_imap_mcp.broker.accounts import AccountConfig, AccountRegistry
from readndraft_imap_mcp.broker.service import BrokerService
from readndraft_imap_mcp.imap.client import ImapClient
from readndraft_imap_mcp.ipc import BrokerRpcServer, IpcBrokerClient
from readndraft_imap_mcp.platform.paths import AppPaths


class SyntheticCredentials:
    async def load_secret(self, account_id: str) -> str:
        assert account_id == "synthetic"
        return "synthetic-secret"

    async def save_secret(self, account_id: str, secret: str) -> None:
        raise AssertionError("the isolated read test must not save credentials")

    async def delete_secret(self, account_id: str) -> None:
        raise AssertionError("the isolated read test must not delete credentials")


class ScriptedImapConnection:
    """Minimal in-memory IMAP transport for the real client protocol calls."""

    def __init__(self) -> None:
        self.commands: list[tuple[object, ...]] = []

    def select(self, mailbox: str, readonly: bool = False):
        self.commands.append(("EXAMINE", mailbox, readonly))
        assert (mailbox, readonly) == ('"INBOX"', True)
        return "OK", [b"1"]

    def response(self, name: str):
        assert name in {"UIDVALIDITY", "UIDNEXT"}
        return name, [b"42" if name == "UIDVALIDITY" else b"8"]

    def uid(self, *args: object):
        self.commands.append(args)
        command, uid_set, query, *_ = args
        if command == "SEARCH":
            assert uid_set is None and query == "ALL"
            return "OK", [b"7"]
        assert command == "FETCH" and uid_set == "7"
        assert isinstance(query, str)
        if query == "(UID FLAGS)":
            return "OK", [(b"1 (UID 7 FLAGS ())", b"")]
        if "INTERNALDATE" in query:
            assert "BODY.PEEK[]" not in query
            return "OK", [
                (
                    b'1 (UID 7 RFC822.SIZE 4 INTERNALDATE "22-Jul-2026 11:30:00 +0800" '
                    b"BODY[HEADER.FIELDS] {22}",
                    b"Subject: synthetic\r\n\r\n",
                ),
                b")",
            ]
        if "BODYSTRUCTURE" in query:
            return "OK", [
                (
                    b'1 (UID 7 FLAGS () RFC822.SIZE 4 BODYSTRUCTURE '
                    b'("TEXT" "PLAIN" NIL NIL NIL "7BIT" 4 1 NIL NIL NIL NIL) '
                    b"BODY[HEADER.FIELDS] {22}",
                    b"Subject: synthetic\r\n\r\n",
                ),
                b")",
            ]
        assert query == "(UID BODY.PEEK[1])"
        return "OK", [(b"1 (UID 7 BODY[1] {4}", b"body"), b")"]


class ScriptedImapClient(ImapClient):
    def __init__(self, account: AccountConfig, secret: str, connection: ScriptedImapConnection) -> None:
        super().__init__(account, secret)
        self._scripted_connection = connection

    def __enter__(self) -> ScriptedImapClient:
        self.connection = self._scripted_connection  # type: ignore[assignment]
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.connection = None

    @property
    def imap(self):
        if self.connection is None:
            raise AssertionError("scripted IMAP connection was not entered")
        return self.connection


def _paths(tmp_path) -> AppPaths:
    if sys.platform == "win32":
        root = tmp_path / "readNdraft"
        return AppPaths(root / "config", root / "state", root / "runtime", root)
    return AppPaths(
        tmp_path / "config" / "readndraft",
        tmp_path / "state" / "readndraft",
        tmp_path / "runtime" / "readndraft",
        tmp_path / "data" / "readndraft",
    )


def _frontend_environment(tmp_path) -> dict[str, str]:
    environment = os.environ.copy()
    if sys.platform == "win32":
        environment["LOCALAPPDATA"] = str(tmp_path.resolve())
    else:
        environment["XDG_CONFIG_HOME"] = str((tmp_path / "config").resolve())
        environment["XDG_STATE_HOME"] = str((tmp_path / "state").resolve())
        environment["XDG_RUNTIME_DIR"] = str((tmp_path / "runtime").resolve())
        environment["XDG_DATA_HOME"] = str((tmp_path / "data").resolve())
    return environment


async def _call_stdio(tmp_path) -> tuple[dict, dict]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "readndraft_imap_mcp.mcp_server.server"],
        env=_frontend_environment(tmp_path),
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await asyncio.wait_for(session.initialize(), timeout=10)
            search = await asyncio.wait_for(
                session.call_tool(
                    "search_emails",
                    {"targets": [{"account_id": "synthetic", "mailbox": "INBOX"}], "limit": 1},
                ),
                timeout=10,
            )
            assert search.isError is False, search.content
            assert search.structuredContent["results"], search.structuredContent
            identity = search.structuredContent["results"][0]["identity"]
            message = await asyncio.wait_for(session.call_tool("get_email", identity), timeout=10)
            assert message.isError is False, message.content
            return search.structuredContent, message.structuredContent


def test_isolated_mcp_stdio_to_real_broker_and_selective_imap_read(tmp_path) -> None:
    """Exercise stdio MCP -> authenticated IPC -> broker -> real client without TLS/network."""
    paths = _paths(tmp_path)
    key = paths.load_or_create_ipc_key()
    connection = ScriptedImapConnection()
    account = AccountConfig("synthetic", "synthetic.invalid", 993, "synthetic@example.test")
    broker = BrokerService(
        accounts=AccountRegistry((account,)),
        credentials=SyntheticCredentials(),
        client_factory=lambda configured, secret: ScriptedImapClient(configured, secret, connection),
        request_timeout_seconds=10,
    )
    server = BrokerRpcServer(broker, paths.ipc_address, key)
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            server.serve_forever()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve, name="isolated-readndraft-broker", daemon=True)
    thread.start()
    client = IpcBrokerClient(paths.ipc_address, key)
    deadline = time.monotonic() + 10
    try:
        while True:
            try:
                health = client.health()
                break
            except (ConnectionError, FileNotFoundError, OSError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise AssertionError("isolated broker did not become healthy")
                time.sleep(0.02)
        assert health["status"] == "healthy"
        search, message = asyncio.run(_call_stdio(tmp_path))
    finally:
        server.request_shutdown()
        thread.join(timeout=10)

    assert not thread.is_alive(), "isolated broker did not shut down"
    assert not errors
    assert search["results"] == [
        {
            "identity": {"account_id": "synthetic", "mailbox": "INBOX", "uid_validity": "42", "uid": "7"},
            "headers": {"subject": "synthetic"},
            "flags": [],
            "size": 4,
            "received_at": "2026-07-22T03:30:00Z",
        }
    ]
    assert search["target_statuses"] == [
        {"account_id": "synthetic", "mailbox": "INBOX", "status": "complete", "cursor": None, "error": None}
    ]
    assert message["identity"] == search["results"][0]["identity"]
    assert message["text"] == "body"
    assert message["flags"] == []
    assert message["attachments"] == []
    assert any(command[0] == "EXAMINE" for command in connection.commands)
    fetches = [command[2] for command in connection.commands if command[0] == "FETCH"]
    assert any("BODYSTRUCTURE" in query for query in fetches)
    assert "(UID BODY.PEEK[1])" in fetches
    assert all("BODY.PEEK[]" not in query for query in fetches)
    assert all(command[0] != "STORE" for command in connection.commands)
