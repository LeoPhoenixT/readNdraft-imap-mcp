"""Exercise the actual platform transport with synthetic authentication/data only."""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import threading
import time
from dataclasses import replace
from multiprocessing.connection import Client
from pathlib import Path

import pytest

from readndraft_imap_mcp.broker.settings import DEFAULT_CAPACITY
from readndraft_imap_mcp.ipc.client import IpcBrokerClient
from readndraft_imap_mcp.ipc.codec import _encode
from readndraft_imap_mcp.ipc.contract import RpcError
from readndraft_imap_mcp.ipc.transport import BrokerTransportRuntime


def frame(operation="health"):
    return _encode({"request_id": "0" * 32, "operation": operation, "params": {}})


def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("transport state did not reach expected barrier")
        threading.Event().wait(0.01)


@pytest.fixture
def runtime(monkeypatch):
    import readndraft_imap_mcp.ipc.transport as transport

    ready = threading.Event()
    dispatch_entered = threading.Event()
    dispatch_release = threading.Event()
    original_listener = transport.Listener

    def listen(*args, **kwargs):
        listener = original_listener(*args, **kwargs)
        ready.set()
        return listener

    monkeypatch.setattr(transport, "Listener", listen)

    async def dispatch(operation, params):
        if operation == "list_accounts":
            import asyncio

            dispatch_entered.set()
            await asyncio.to_thread(dispatch_release.wait, 5)
            return []
        return {"synthetic": True}

    with tempfile.TemporaryDirectory(prefix="rnd-", dir=None if sys.platform == "win32" else "/tmp") as directory:
        address = (
            rf"\\.\pipe\readndraft-synthetic-{os.getpid()}-{time.monotonic_ns()}"
            if sys.platform == "win32"
            else str(Path(directory) / "b.sock")
        )
        server = BrokerTransportRuntime(
            dispatch,
            address,
            b"synthetic-test-key",
            capacity_settings=replace(
                DEFAULT_CAPACITY,
                ipc_connections=6,
                ipc_rpc=1,
                ipc_leases=1,
                ipc_handshake_seconds=0.3,
                ipc_frame_seconds=0.3,
            ),
        )
        failure = []

        def serve():
            try:
                server.serve_forever()
            except BaseException as exc:
                failure.append(exc)
                ready.set()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        assert ready.wait(5)
        if failure:
            raise failure[0]
        try:
            yield server, dispatch_entered, dispatch_release
        finally:
            dispatch_release.set()
            server.request_shutdown()
            thread.join(5)
            assert not thread.is_alive()
            assert not failure
            eventually(lambda: server.resource_snapshot()["ipc_connections"] == 0)


def connect(server):
    return Client(server.address, family=server.family, authkey=server.authkey)


def request(server, operation="health"):
    with connect(server) as connection:
        connection.send_bytes(frame(operation))
        assert connection.poll(5)
        return json.loads(connection.recv_bytes())


def test_rpc_and_lease_caps_leave_control_capacity_and_recover(runtime):
    server, entered, release = runtime
    with connect(server) as lease, connect(server) as busy:
        lease.send_bytes(frame("frontend_lease"))
        assert lease.poll(5)
        assert json.loads(lease.recv_bytes())["result"] == {"leased": True}
        busy.send_bytes(frame("list_accounts"))
        assert entered.wait(5)
        assert request(server)["ok"]
        assert request(server, "list_accounts")["error"]["reason"] == "ipc_connection_capacity"
        with pytest.raises(RpcError) as error, IpcBrokerClient(server.address, server.authkey).frontend_lease():
            pytest.fail("the broker granted a lease beyond its capacity")
        assert error.value.code == "rate_limited"
        assert error.value.reason == "ipc_connection_capacity"
        usage = server.resource_snapshot()
        assert usage["ipc_rpc"] == usage["ipc_leases"] == 1
        assert usage["ipc_rejections"]["rpc"] == usage["ipc_rejections"]["leases"] == 1
        release.set()
        assert busy.poll(5)
        assert json.loads(busy.recv_bytes())["ok"]
    eventually(lambda: server.resource_snapshot()["ipc_connections"] == 0)
    assert request(server, "list_accounts")["ok"]


def test_stalled_authentication_does_not_block_accept_and_is_bounded(runtime):
    server, _, _ = runtime
    with Client(server.address, family=server.family, authkey=None) as stalled:
        # Read the challenge but never return the authentication response.
        assert stalled.poll(5)
        assert stalled.recv_bytes()
        assert request(server)["ok"]
        eventually(lambda: server.resource_snapshot()["ipc_rejections"]["handshake_timeout"] == 1)
    eventually(lambda: server.resource_snapshot()["ipc_connections"] == 0)


@pytest.mark.parametrize("partial", [b"", b"\x00", struct.pack("!i", 100) + b"partial"])
def test_initial_deadline_covers_complete_frame_including_partial_header_and_body(runtime, partial):
    server, _, _ = runtime
    if server.family != "AF_UNIX" and partial:
        pytest.skip("Unix byte-stream frame injection does not apply to message-mode named pipes")
    with connect(server) as stalled:
        if partial:
            os.write(stalled.fileno(), partial)
        assert request(server)["ok"]
        eventually(lambda: server.resource_snapshot()["ipc_rejections"]["frame_timeout"] == 1)
    eventually(lambda: server.resource_snapshot()["ipc_connections"] == 0)
    assert request(server)["ok"]


def test_total_connection_admission_has_fixed_worker_bound(runtime):
    server, _, _ = runtime
    peers = []
    try:
        for _ in range(6):
            peer = Client(server.address, family=server.family, authkey=None)
            assert peer.poll(5)
            assert peer.recv_bytes()
            peers.append(peer)
        assert server.resource_snapshot()["ipc_connections"] == 6
        with Client(server.address, family=server.family, authkey=None) as rejected:
            with pytest.raises((EOFError, OSError)):
                # Windows can report a broken pipe while checking readiness.
                assert rejected.poll(5)
                rejected.recv_bytes()
        assert server.resource_snapshot()["ipc_connections"] <= 6
        assert server.resource_snapshot()["ipc_rejections"]["connection"] == 1
    finally:
        for peer in peers:
            peer.close()
    eventually(lambda: server.resource_snapshot()["ipc_connections"] == 0)
    assert request(server)["ok"]


def test_shutdown_control_acknowledges_and_drains_saturated_rpc_and_lease(runtime):
    server, entered, release = runtime
    with connect(server) as lease, connect(server) as busy:
        lease.send_bytes(frame("frontend_lease"))
        assert lease.poll(5)
        assert json.loads(lease.recv_bytes())["ok"]
        busy.send_bytes(frame("list_accounts"))
        assert entered.wait(5)
        assert request(server, "shutdown")["ok"]
        assert not server._shutdown.is_set()
        release.set()
        assert busy.poll(5)
        assert json.loads(busy.recv_bytes())["ok"]
        assert not server._shutdown.is_set()  # The lease still owns its lifetime.
    eventually(server._shutdown.is_set)
