from __future__ import annotations

import asyncio
import json
import os
import platform
import threading
import time
from pathlib import Path

import pytest

from readndraft_imap_mcp import __version__
from readndraft_imap_mcp.broker.limits import RequestQuotaError
from readndraft_imap_mcp.drafts import DraftBusyError, DraftRecoveryRequiredError
from readndraft_imap_mcp.imap.client import ImapClientError, ImapMovePartialError
from readndraft_imap_mcp.imap.models import (
    BatchMessageContent,
    BatchMoveResult,
    Mailbox,
    MailboxBatchResult,
    MessageContent,
    MessageIdentity,
    MoveResult,
)
from readndraft_imap_mcp.ipc import client as rpc_module
from readndraft_imap_mcp.ipc.rpc import (
    BrokerRpcServer,
    IpcBrokerClient,
    RpcError,
    _decode_request,
    _encode,
    _json_kwargs,
    _safe_error,
)
from readndraft_imap_mcp.mime.html import AuthoredHtmlError
from readndraft_imap_mcp.protocol_version import IPC_PROTOCOL_VERSION


class FakeBroker:
    def list_accounts(self):
        return [{"id": "personal", "username": "l***@example.com"}]


def _frame(operation: str, params: dict) -> bytes:
    return _encode(
        {
            "request_id": "0" * 32,
            "operation": operation,
            "params": params,
        }
    )


def test_safe_error_passes_through_authored_html_detail() -> None:
    code, message = _safe_error(AuthoredHtmlError("unsupported draft HTML attribute: ping"))
    assert code == "invalid_request" and "ping" in message


def test_safe_error_still_redacts_plain_value_error() -> None:
    assert _safe_error(ValueError("/home/user/secret/path")) == (
        "invalid_request",
        "request rejected",
    )


SEARCH_FILTERS = {
    "sender": None,
    "recipient": None,
    "subject": None,
    "text": None,
    "attachment_filename": None,
    "after": None,
    "before": None,
    "read": None,
    "starred": None,
}


@pytest.mark.parametrize(
    ("exception", "code"),
    ((DraftBusyError("private"), "draft_busy"), (DraftRecoveryRequiredError("private"), "recovery_required")),
)
def test_rpc_maps_draft_recovery_errors_without_details(exception, code) -> None:
    class FailingBroker:
        def list_accounts(self):
            raise exception

    response = json.loads(BrokerRpcServer(FailingBroker(), "unused", b"x").handle_frame(_frame("list_accounts", {})))
    assert response["error"]["type"] == code
    assert "private" not in repr(response)


def test_async_write_transport_loss_is_outcome_unknown() -> None:
    class LostClient(IpcBrokerClient):
        def __init__(self):
            pass

        def _request_sync(self, operation, params):
            raise RpcError("private transport detail", code="connection_error")

    with pytest.raises(RpcError, match="outcome_unknown") as exc:
        asyncio.run(LostClient().update_draft("personal", "a" * 32, to=("a@example.com",), subject="s", body="b"))
    assert "private" not in str(exc.value)


def test_rpc_health_and_account_list_are_json_only() -> None:
    server = BrokerRpcServer(FakeBroker(), "/tmp/not-used.sock", b"x" * 32)

    health = json.loads(server.handle_frame(_frame("health", {})))
    accounts = json.loads(server.handle_frame(_frame("list_accounts", {})))

    assert health["ok"] is True
    assert health["result"]["status"] == "healthy"
    assert health["result"]["protocol_version"] == IPC_PROTOCOL_VERSION
    assert health["result"]["package_version"] == __version__
    assert health["result"]["python_version"] == platform.python_version()
    assert health["result"]["python_implementation"] == platform.python_implementation()
    assert health["result"]["pid"] == os.getpid()
    assert accounts["result"][0]["id"] == "personal"


def test_frontend_lease_is_an_exact_authenticated_operation() -> None:
    request = _decode_request(_frame("frontend_lease", {}))
    assert request["operation"] == "frontend_lease"
    with pytest.raises(ValueError, match="invalid RPC parameters"):
        _decode_request(_frame("frontend_lease", {"idle_timeout": 999}))


def test_frontend_lease_counts_as_active_until_disconnect() -> None:
    class LeaseConnection:
        def __init__(self) -> None:
            self.waiting = threading.Event()
            self.release = threading.Event()
            self.sent: list[bytes] = []
            self.receives = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def recv_bytes(self, maximum):
            self.receives += 1
            if self.receives == 1:
                return _frame("frontend_lease", {})
            self.waiting.set()
            self.release.wait(timeout=1)
            raise EOFError

        def send_bytes(self, value):
            self.sent.append(value)

    connection = LeaseConnection()
    server = BrokerRpcServer(FakeBroker(), "unused", b"x")
    worker = threading.Thread(target=server._transport._serve_connection, args=(connection,))
    worker.start()
    assert connection.waiting.wait(timeout=1)
    assert server._transport._active_clients == 1
    response = json.loads(connection.sent[0])
    assert response["result"] == {"leased": True}

    connection.release.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert server._transport._active_clients == 0


def test_rpc_rejects_unknown_operation_before_dispatch() -> None:
    raw = _encode(
        {"request_id": "0" * 32, "operation": "send_email", "params": {}}
    )
    response = json.loads(BrokerRpcServer(FakeBroker(), "unused", b"x").handle_frame(raw))

    assert response["ok"] is False
    assert response["request_id"] == "0" * 32


def test_rpc_rejects_missing_and_extra_parameters() -> None:
    for params in ({}, {"account_id": "personal", "host": "attacker.example"}):
        raw = _frame("list_mailboxes", params)
        response = json.loads(BrokerRpcServer(FakeBroker(), "unused", b"x").handle_frame(raw))
        assert response["ok"] is False
        assert response["error"]["message"] == "request rejected"


def test_rpc_parameter_rejection_echoes_valid_request_id() -> None:
    request_id = "1" * 32
    raw = _encode(
        {
            "request_id": request_id,
            "operation": "list_mailboxes",
            "params": {
                "account_id": "personal",
                "definitely_not_a_param": 1,
            },
        }
    )

    response = json.loads(
        BrokerRpcServer(FakeBroker(), "unused", b"x").handle_frame(raw)
    )

    assert response["request_id"] == request_id
    assert response["ok"] is False
    assert response["error"]["type"] == "invalid_request"


@pytest.mark.parametrize(
    ("operation", "params"),
    (
        ("search_emails", {"account_id": "a", "mailbox": "INBOX", "filters": {}}),
        (
            "search_emails",
            {"account_id": "a", "mailbox": "INBOX", "filters": {**SEARCH_FILTERS, "after": "2026-02-30"}},
        ),
        (
            "search_emails",
            {"account_id": "a", "mailbox": "INBOX", "filters": {**SEARCH_FILTERS, "read": 1}},
        ),
        ("search_emails", {"account_id": "a", "mailbox": "INBOX", "filters": SEARCH_FILTERS, "limit": True}),
        ("search_emails", {"account_id": "a", "mailbox": "INBOX", "filters": SEARCH_FILTERS, "limit": 0}),
        ("search_email_targets", {"targets": [], "filters": SEARCH_FILTERS}),
        ("search_email_targets", {"targets": [["a", ""]], "filters": SEARCH_FILTERS}),
        (
            "search_email_targets",
            {"targets": [["a", "INBOX"]], "filters": SEARCH_FILTERS, "cursor": ""},
        ),
        (
            "search_email_targets",
            {"targets": [["a", "INBOX"]], "filters": SEARCH_FILTERS, "cursor": "游標"},
        ),
        ("list_mailboxes", {"account_ids": []}),
        ("get_emails", {"identities": []}),
    ),
)
def test_rpc_rejects_malformed_frames_before_broker_dispatch(operation: str, params: dict) -> None:
    class DispatchTrackingBroker:
        def __getattr__(self, name):
            raise AssertionError(f"broker dispatch reached {name}")

    response = json.loads(
        BrokerRpcServer(DispatchTrackingBroker(), "unused", b"x").handle_frame(_frame(operation, params))
    )

    assert response["ok"] is False
    assert response["error"]["type"] == "invalid_request"


@pytest.mark.parametrize("filename", ("報告.pdf", "résumé.txt", "招待状（最終版）.pdf"))
def test_rpc_accepts_unicode_attachment_filename_filter(filename: str) -> None:
    decoded = _decode_request(
        _frame(
            "search_email_targets",
            {
                "targets": [["personal", "INBOX"]],
                "filters": {**SEARCH_FILTERS, "attachment_filename": filename},
                "limit": 1,
                "cursor": None,
            },
        )
    )

    assert decoded["params"]["filters"]["attachment_filename"] == filename


def test_rpc_accepts_nullable_reply_identity_and_empty_recipient_lists() -> None:
    decoded = _decode_request(
        _frame(
            "create_draft",
            {"account_id": "a", "to": [], "cc": [], "bcc": [], "subject": "s", "body": "b", "reply_to_message": None},
        )
    )

    assert decoded["params"] == {"account_id": "a", "to": [], "cc": [], "bcc": [], "subject": "s", "body": "b"}


def test_rpc_mailbox_batch_and_text_preview_round_trip() -> None:
    identity = MessageIdentity("personal", "INBOX", "42", "7")

    class ReadBroker(FakeBroker):
        async def list_mailboxes_batch(self, account_ids):
            return tuple(
                MailboxBatchResult(account_id, True, (Mailbox("INBOX", "/", ()),))
                for account_id in account_ids
            )

        async def get_email(self, requested, max_text_chars=None):
            assert (requested, max_text_chars) == (identity, 3)
            return MessageContent(identity, {}, "abc", (), (), 10, 6, True)

        async def get_emails(self, identities, max_text_chars=None):
            return (BatchMessageContent(identities[0], True, await self.get_email(identities[0], max_text_chars)),)

    server = BrokerRpcServer(ReadBroker(), "unused", b"x")
    mailbox = json.loads(server.handle_frame(_frame("list_mailboxes", {"account_ids": ["personal"]})))
    assert mailbox["result"][0]["mailboxes"][0]["name"] == "INBOX"
    params = {
        "identity": {
            "account_id": "personal",
            "mailbox": "INBOX",
            "uid_validity": "42",
            "uid": "7",
        },
        "max_text_chars": 3,
    }
    single = json.loads(server.handle_frame(_frame("get_email", params)))
    assert single["result"]["text_total_chars"] == 6
    batch = json.loads(
        server.handle_frame(
            _frame(
                "get_emails",
                {"identities": [params["identity"]], "max_text_chars": 3},
            )
        )
    )
    assert batch["result"][0]["message"]["text_truncated"] is True

    class CapturingClient(IpcBrokerClient):
        def __init__(self):
            pass

        async def _request(self, operation, request):
            assert operation == "get_email"
            assert request["max_text_chars"] == 3
            return single["result"]

    message = asyncio.run(CapturingClient().get_email(identity, 3))
    assert (message.text, message.text_total_chars, message.text_truncated) == ("abc", 6, True)


@pytest.mark.parametrize("value", [True, 0, 100001])
def test_rpc_rejects_invalid_text_preview(value) -> None:
    identity = {"account_id": "personal", "mailbox": "INBOX", "uid_validity": "42", "uid": "7"}
    with pytest.raises(ValueError, match="invalid RPC parameter type"):
        _decode_request(_frame("get_email", {"identity": identity, "max_text_chars": value}))


def test_rpc_rejects_type_confused_writes() -> None:
    server = BrokerRpcServer(FakeBroker(), "unused", b"x")
    for operation, params in (
        (
            "set_star",
            {
                "identity": {
                    "account_id": "a",
                    "mailbox": "INBOX",
                    "uid_validity": "1",
                    "uid": "2",
                },
                "enabled": "false",
            },
        ),
        ("create_draft", {"account_id": "a", "to": "x@example.com", "subject": "s", "body": "b"}),
        ("create_draft", {"account_id": "a", "to": ["x@example.com"], "subject": "s", "body": "b", "html_body": 7}),
    ):
        response = json.loads(server.handle_frame(_frame(operation, params)))
        assert response["ok"] is False
        assert response["error"]["type"] == "invalid_request"


def test_rpc_accepts_optional_html_body() -> None:
    base = {"account_id": "a", "to": ["x@example.com"], "subject": "s", "body": "b"}
    decoded = _decode_request(
        _frame("create_draft", {**base, "html_body": "<p>b</p>"})
    )
    assert decoded["params"]["html_body"] == "<p>b</p>"
    assert _decode_request(_frame("create_draft", {**base, "html_body": None}))["params"]["html_body"] is None


def test_rpc_accepts_only_a_complete_reply_identity() -> None:
    base = {"account_id": "a", "to": ["x@example.com"], "subject": "s", "body": "b"}
    identity = {"account_id": "a", "mailbox": "INBOX", "uid_validity": "1", "uid": "2"}
    decoded = _decode_request(
        _frame("create_draft", {**base, "reply_to_message": identity})
    )
    assert decoded["params"]["reply_to_message"] == identity
    with pytest.raises(ValueError, match="invalid message identity"):
        _decode_request(_frame("create_draft", {**base, "reply_to_message": {"account_id": "a"}}))


def test_rpc_omits_absent_reply_identity_and_serializes_present_identity() -> None:
    identity = MessageIdentity("a", "INBOX", "1", "2")
    assert _json_kwargs({"reply_to_message": None}) == {}
    assert _json_kwargs({"reply_to_message": identity}) == {
        "reply_to_message": {
            "account_id": "a", "mailbox": "INBOX", "uid_validity": "1", "uid": "2"
        }
    }


def test_rpc_move_contract_is_exact_and_serializes_results() -> None:
    identity = {
        "account_id": "personal",
        "mailbox": "INBOX",
        "uid_validity": "42",
        "uid": "7",
    }

    class MoveBroker(FakeBroker):
        async def move_email(self, source, destination_mailbox, client_id=None):
            destination = MessageIdentity("personal", destination_mailbox, "77", "99")
            return MoveResult(source, destination_mailbox, destination)

        async def move_emails_batch(
            self, identities, destination_mailbox, client_id=None
        ):
            move = await self.move_email(identities[0], destination_mailbox, client_id)
            return (BatchMoveResult(identities[0], True, move),)

    server = BrokerRpcServer(MoveBroker(), "unused", b"x")
    single = json.loads(
        server.handle_frame(
            _frame(
                "move_email",
                {"identity": identity, "destination_mailbox": "Archive"},
            )
        )
    )
    assert single["result"]["destination_identity"]["uid"] == "99"

    batch = json.loads(
        server.handle_frame(
            _frame(
                "move_emails_batch",
                {"identities": [identity], "destination_mailbox": "Archive"},
            )
        )
    )
    assert batch["result"][0]["ok"] is True

    for params in (
        {"identity": identity},
        {"identity": identity, "destination_mailbox": 7},
        {
            "identity": identity,
            "destination_mailbox": "Archive",
            "source_mailbox": "INBOX",
        },
    ):
        rejected = json.loads(server.handle_frame(_frame("move_email", params)))
        assert rejected["ok"] is False
        assert rejected["error"]["type"] == "invalid_request"


def test_rpc_does_not_return_internal_exception_details() -> None:
    class FailingBroker:
        def list_accounts(self):
            raise RuntimeError("secret=/private/path/password")

    response = json.loads(
        BrokerRpcServer(FailingBroker(), "unused", b"x").handle_frame(
            _frame("list_accounts", {})
        )
    )

    assert response["error"] == {
        "type": "broker_error",
        "message": "broker request failed",
    }
    assert "private" not in repr(response)


@pytest.mark.parametrize(
    ("exception", "code", "message"),
    (
        (TimeoutError("private timeout detail"), "timeout", "broker request timed out"),
        (RequestQuotaError("private quota detail"), "rate_limited", "account request limit exceeded"),
        (ImapClientError("private IMAP detail"), "imap_error", "IMAP operation failed"),
        (
            ImapMovePartialError("private partial detail"),
            "partial_move",
            "move may have copied the message; inspect both mailboxes",
        ),
        (OSError("private socket detail"), "connection_error", "mail server connection failed"),
    ),
)
def test_rpc_returns_typed_safe_operational_errors(exception, code, message) -> None:
    class FailingBroker:
        def list_accounts(self):
            raise exception

    response = json.loads(
        BrokerRpcServer(FailingBroker(), "unused", b"x").handle_frame(
            _frame("list_accounts", {})
        )
    )

    assert response["error"] == {"type": code, "message": message}
    assert "private" not in repr(response)


def test_rpc_request_shape_is_exact() -> None:
    raw = _encode(
        {
            "request_id": "0" * 32,
            "operation": "health",
            "params": {},
            "unexpected": True,
        }
    )

    try:
        _decode_request(raw)
    except ValueError as exc:
        assert str(exc) == "invalid RPC request shape"
    else:
        raise AssertionError("unexpected field was accepted")


def test_launcher_owned_server_waits_for_idle_and_grace() -> None:
    server = BrokerRpcServer(
        FakeBroker(),
        "/tmp/not-used.sock",
        b"x" * 32,
        idle_timeout_seconds=0.05,
        shutdown_grace_seconds=0.05,
    )
    server._transport._client_started()
    watcher = threading.Thread(target=server._transport._idle_watchdog)
    watcher.start()
    time.sleep(0.12)
    assert server._transport._shutdown.is_set() is False

    server._transport._client_finished()
    watcher.join(timeout=1)
    assert server._transport._shutdown.is_set() is True


def test_invalid_idle_lifecycle_values_fail_closed() -> None:
    with pytest.raises(ValueError, match="idle timeout"):
        BrokerRpcServer(FakeBroker(), "unused", b"x", idle_timeout_seconds=0)
    with pytest.raises(ValueError, match="shutdown grace"):
        BrokerRpcServer(FakeBroker(), "unused", b"x", shutdown_grace_seconds=-1)


def test_stale_socket_cleanup_refuses_non_socket_path(tmp_path) -> None:
    endpoint = (tmp_path / "broker.sock").resolve()
    endpoint.write_text("do not delete", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not a socket"):
        BrokerRpcServer._unix_endpoint_in_use(Path(endpoint))
    assert endpoint.read_text(encoding="utf-8") == "do not delete"


def test_frontend_lease_bounds_delayed_connect_and_reply(monkeypatch) -> None:
    class DelayedConnection:
        def send_bytes(self, value):
            return None

        def poll(self, timeout):
            time.sleep(0.1)
            return False

        def close(self):
            return None

    def delayed_client(*args, **kwargs):
        time.sleep(0.1)
        return DelayedConnection()

    monkeypatch.setattr(rpc_module, "Client", delayed_client)
    monkeypatch.setattr(rpc_module, "RPC_RESPONSE_TIMEOUT_SECONDS", 0.02)
    client = IpcBrokerClient("unused", b"x")
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        with client.frontend_lease():
            pass
    assert time.monotonic() - started < 0.06


def test_frontend_lease_bounds_blocked_send(monkeypatch) -> None:
    release = threading.Event()

    class BlockingConnection:
        def send_bytes(self, value):
            release.wait(timeout=1)

        def close(self):
            return None

    monkeypatch.setattr(rpc_module, "Client", lambda *args, **kwargs: BlockingConnection())
    monkeypatch.setattr(rpc_module, "RPC_RESPONSE_TIMEOUT_SECONDS", 0.02)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            with IpcBrokerClient("unused", b"x").frontend_lease():
                pass
        assert time.monotonic() - started < 0.06
    finally:
        release.set()


def test_runtime_loop_stops_after_requested_shutdown() -> None:
    server = BrokerRpcServer(FakeBroker(), "unused", b"x")
    server.handle_frame(_frame("health", {}))
    loop = server._transport._runtime_loop
    assert loop is not None
    server.request_shutdown()
    deadline = time.monotonic() + 1
    while not loop.is_closed() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert loop.is_closed()
