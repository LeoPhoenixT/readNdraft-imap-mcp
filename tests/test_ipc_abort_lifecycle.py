"""Deterministic broker abort ownership, including simulated Windows overlapped I/O."""

from __future__ import annotations

import ast
import inspect
import io
import json
import threading
import time
from multiprocessing import connection as stdlib_connection
from types import SimpleNamespace

import pytest

from readndraft_imap_mcp.ipc import transport
from readndraft_imap_mcp.ipc.codec import _encode
from readndraft_imap_mcp.ipc.transport import (
    _PIPE_CANCELLED_READ,
    BrokerTransportRuntime,
    _ConnectionAborted,
    _ConnectionState,
)


def _frame():
    return _encode({"request_id": "0" * 32, "operation": "health", "params": {}})


class Connection:
    _got_empty_message = False
    _handle = 1

    def __init__(self, stage="frame", error=None):
        self.stage = stage
        self.error = error
        self.entered = threading.Event()
        self.release = threading.Event()
        self.sent = []
        self.closed = False
        self.pipe_recv = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def blocked(self):
        self.entered.set()
        assert self.release.wait(5), "test did not release synthetic I/O"
        if self.error is not None:
            raise self.error

    def recv_bytes(self, maximum):
        if self.stage in {"response", "success"}:
            return _frame()
        if self.pipe_recv is not None:
            return self.pipe_recv(self, maximum)
        self.blocked()
        return _frame()

    def send_bytes(self, raw):
        if self.stage == "response":
            self.blocked()
        self.sent.append(raw)


@pytest.fixture
def runtime(monkeypatch):
    async def dispatch(operation, params):
        raise AssertionError("synthetic test must not dispatch to a live broker")

    server = BrokerTransportRuntime(dispatch, "unused", b"synthetic")
    server.family = "AF_PIPE"
    monkeypatch.setattr(server, "_abort_connection", lambda connection: connection.release.set())
    monkeypatch.setattr(server, "_wake_listener", lambda: None)
    monkeypatch.setattr(server, "_handle_request", lambda request: _encode({"ok": True}))
    monkeypatch.setattr(transport, "deliver_challenge", lambda connection, key: connection.recv_bytes(256))
    monkeypatch.setattr(transport, "answer_challenge", lambda connection, key: None)
    return server


def register(server, connection):
    assert server._connection_capacity.acquire(blocking=False)
    with server._state_lock:
        server._connections[connection] = _ConnectionState(time.monotonic() + 60, "handshake_timeout")


def start_worker(server, connection):
    register(server, connection)
    failures = []

    def serve():
        try:
            server._serve_connection(connection, authenticate=connection.stage == "handshake")
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=serve)
    worker.start()
    assert connection.entered.wait(5)
    return worker, failures


def expire(server, connection):
    with server._state_lock:
        server._connections[connection].deadline = 0
    server._expire_connections(1)


def assert_released(server):
    usage = server.resource_snapshot()
    assert usage["ipc_connections"] == server._active_clients == 0
    assert usage["ipc_control"] == usage["ipc_rpc"] == usage["ipc_leases"] == 0
    acquired = []
    try:
        for _ in range(server._settings.ipc_connections):
            assert server._connection_capacity.acquire(blocking=False)
            acquired.append(True)
        assert not server._connection_capacity.acquire(blocking=False)
    finally:
        for _ in acquired:
            server._connection_capacity.release()


def assert_unrelated_connection_useful(server):
    connection = Connection("success")
    register(server, connection)
    server._serve_connection(connection)
    assert json.loads(connection.sent[0]) == {"ok": True}
    assert connection.closed
    assert_released(server)


def cancelled_pipe_read(connection):
    """Execute installed CPython's Windows receive method with fake Win32 I/O.

    The method is present in the stdlib source on POSIX too. This reproduces
    its ERROR_OPERATION_ABORTED result without claiming native Windows proof.
    """
    source = inspect.getsource(stdlib_connection)
    pipe_class = next(
        node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ClassDef) and node.name == "PipeConnection"
    )
    method = next(node for node in pipe_class.body if isinstance(node, ast.FunctionDef) and node.name == "_recv_bytes")

    class Overlapped:
        event = 1

        def GetOverlappedResult(self, wait):
            return 0, 995  # ERROR_OPERATION_ABORTED after CancelIoEx.

        def cancel(self):
            pass

    def wait_for_io(*args):
        connection.blocked()
        return 0  # WAIT_OBJECT_0

    api = SimpleNamespace(
        ERROR_IO_PENDING=997,
        ERROR_OPERATION_ABORTED=995,
        ERROR_MORE_DATA=234,
        ERROR_BROKEN_PIPE=109,
        ReadFile=lambda *args, **kwargs: (Overlapped(), 997),
        WaitForMultipleObjects=wait_for_io,
    )
    namespace = {"_winapi": api, "io": io, "INFINITE": -1, "WAIT_OBJECT_0": 0}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<stdlib PipeConnection._recv_bytes>", "exec"), namespace)
    return namespace["_recv_bytes"]


@pytest.mark.parametrize("stage", ["handshake", "frame"])
def test_windows_stdlib_cancelled_read_is_normalized_and_counted_once(runtime, stage):
    connection = Connection(stage)
    connection.pipe_recv = cancelled_pipe_read(connection)
    worker, failures = start_worker(runtime, connection)
    try:
        expire(runtime, connection)
        runtime._expire_connections(1)  # Repeated scans cannot count twice.
    finally:
        connection.release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert failures == []
    counts = runtime.resource_snapshot()["ipc_rejections"]
    assert counts[f"{stage}_timeout"] == 1
    assert counts["frame_timeout" if stage == "handshake" else "handshake_timeout"] == 0
    assert connection.closed
    assert_released(runtime)
    assert_unrelated_connection_useful(runtime)


@pytest.mark.parametrize("error", [EOFError(), OSError("cancelled I/O"), ValueError("closed handle"), TimeoutError()])
@pytest.mark.parametrize("stage", ["handshake", "frame", "response"])
def test_regular_io_abort_paths_release_resources_without_misclassifying_phase(runtime, error, stage):
    connection = Connection(stage, error)
    worker, failures = start_worker(runtime, connection)
    try:
        if stage == "response":
            assert runtime.resource_snapshot()["ipc_control"] == 1
        expire(runtime, connection)
    finally:
        connection.release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert failures == []
    counts = runtime.resource_snapshot()["ipc_rejections"]
    assert counts["handshake_timeout"] == (1 if stage == "handshake" else 0)
    assert counts["frame_timeout"] == (1 if stage == "frame" else 0)
    assert_released(runtime)
    assert_unrelated_connection_useful(runtime)


@pytest.mark.parametrize("stage", ["handshake", "frame"])
def test_shutdown_cancellation_of_windows_read_is_not_a_timeout(runtime, stage):
    connection = Connection(stage)
    connection.pipe_recv = cancelled_pipe_read(connection)
    worker, failures = start_worker(runtime, connection)
    try:
        with runtime._state_lock:
            runtime._connections[connection].deadline = 0
        runtime.request_shutdown()  # Shutdown wins ownership before watchdog.
        runtime._expire_connections(1)
    finally:
        connection.release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert failures == []
    counts = runtime.resource_snapshot()["ipc_rejections"]
    assert counts["handshake_timeout"] == counts["frame_timeout"] == 0
    assert_released(runtime)


@pytest.mark.parametrize(
    "family,error,abort",
    [
        ("AF_PIPE", RuntimeError("unrelated programmer failure"), True),
        ("AF_PIPE", RuntimeError(_PIPE_CANCELLED_READ), False),
        ("AF_UNIX", RuntimeError(_PIPE_CANCELLED_READ), True),
    ],
)
def test_unrelated_runtime_errors_remain_visible_and_release_capacity(runtime, family, error, abort):
    runtime.family = family
    connection = Connection(error=error)
    worker, failures = start_worker(runtime, connection)
    try:
        if abort:
            expire(runtime, connection)
        else:
            connection.release.set()
    finally:
        connection.release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert failures == [error]
    assert_released(runtime)


def test_watchdog_claim_prevents_phase_transition_before_physical_abort(runtime, monkeypatch):
    connection = Connection("handshake", RuntimeError(_PIPE_CANCELLED_READ))
    worker, failures = start_worker(runtime, connection)
    abort_selected = threading.Event()
    finish_abort = threading.Event()

    def delayed_abort(selected):
        abort_selected.set()
        assert finish_abort.wait(5)
        selected.release.set()

    monkeypatch.setattr(runtime, "_abort_connection", delayed_abort)
    watchdog = threading.Thread(target=expire, args=(runtime, connection))
    watchdog.start()
    try:
        assert abort_selected.wait(5)
        with pytest.raises(_ConnectionAborted):
            runtime._set_deadline(connection, 60, "frame_timeout")
        runtime._expire_connections(1)
        runtime.request_shutdown()  # A later shutdown cannot relabel the timeout.
        with runtime._state_lock:
            assert runtime._connections[connection].abort_reason == "handshake_timeout"
        counts = runtime.resource_snapshot()["ipc_rejections"]
        assert counts["handshake_timeout"] == 1
        assert counts["frame_timeout"] == 0
    finally:
        finish_abort.set()
        connection.release.set()
        watchdog.join(5)
        worker.join(5)
    assert not watchdog.is_alive() and not worker.is_alive()
    assert failures == []
    assert_released(runtime)


def test_expired_handshake_cannot_be_reset_before_watchdog_scan(runtime):
    connection = Connection()
    register(runtime, connection)
    with runtime._state_lock:
        runtime._connections[connection].deadline = 0
    with pytest.raises(_ConnectionAborted):
        runtime._set_deadline(connection, 60, "frame_timeout")
    runtime._serve_connection(connection)  # Drains the known abort without I/O.
    counts = runtime.resource_snapshot()["ipc_rejections"]
    assert counts["handshake_timeout"] == 1
    assert counts["frame_timeout"] == 0
    assert_released(runtime)


def test_shutdown_rejects_connection_registered_after_accept_race(runtime):
    runtime.request_shutdown()
    connection = Connection()
    runtime._accept_connection(connection)
    assert connection.closed
    assert_released(runtime)


@pytest.mark.parametrize("error", [EOFError(), OSError("closed"), TimeoutError()])
def test_shutdown_io_errors_release_response_capacity_without_timeout_counts(runtime, error):
    connection = Connection("response", error)
    worker, failures = start_worker(runtime, connection)
    try:
        assert runtime.resource_snapshot()["ipc_control"] == 1
        with runtime._state_lock:
            runtime._connections[connection].deadline = 0
        runtime.request_shutdown()
    finally:
        connection.release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert failures == []
    counts = runtime.resource_snapshot()["ipc_rejections"]
    assert counts["handshake_timeout"] == counts["frame_timeout"] == 0
    assert_released(runtime)
