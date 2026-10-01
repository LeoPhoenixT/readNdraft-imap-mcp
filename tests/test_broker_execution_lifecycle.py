from __future__ import annotations

import asyncio
import threading
import time

import pytest

from readndraft_imap_mcp.broker import AccountConfig, AccountRegistry, BrokerService


class Credentials:
    async def load_secret(self, account_id):
        return "synthetic-test-value"


class Client:
    def __init__(self, account, secret):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def build_broker(client=Client, credentials=None):
    return BrokerService(
        AccountRegistry([AccountConfig("a", "a.invalid", 993, "a@example.invalid")]),
        credentials or Credentials(), client, max_imap_workers=1, max_waiting_imap_work=0,
    )


async def invoke(broker, batch):
    if batch:
        return await broker._batch_client_call("a", (1,), lambda client, item: item, max_items=1)
    return await broker._client_call("a", lambda client: 1)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("stage", ["credential", "factory", "entry", "exit", "submit"])
def test_lifecycle_failures_release_session_and_capacity(batch, stage, monkeypatch):
    class FailingCredentials(Credentials):
        async def load_secret(self, account_id):
            if stage == "credential":
                raise RuntimeError("synthetic credential failure")
            return await super().load_secret(account_id)

    class FailingClient(Client):
        def __init__(self, account, secret):
            if stage == "factory":
                raise RuntimeError("synthetic factory failure")

        def __enter__(self):
            if stage == "entry":
                raise RuntimeError("synthetic entry failure")
            return self

        def __exit__(self, *args):
            if stage == "exit":
                raise RuntimeError("synthetic exit failure")

    broker = build_broker(FailingClient, FailingCredentials())
    if stage == "submit":
        def rejected(*args, **kwargs):
            raise RuntimeError("synthetic submission failure")
        monkeypatch.setattr(broker._execution._executor, "submit", rejected)
    with pytest.raises(RuntimeError, match="synthetic"):
        asyncio.run(invoke(broker, batch))
    assert broker.resource_snapshot()["resource_usage"]["active_sessions"] == 0
    monkeypatch.undo()
    broker._execution._client_factory = Client
    broker._execution._credentials = Credentials()
    assert asyncio.run(broker._client_call("a", lambda client: "available")) == "available"


@pytest.mark.parametrize("batch", [False, True])
def test_cleanup_submission_failure_still_closes_and_releases(batch, monkeypatch):
    closed = threading.Event()

    class ClosingClient(Client):
        def __exit__(self, *args):
            closed.set()

    broker = build_broker(ClosingClient)
    original = broker._execution._executor.submit
    calls = 0

    def submit(operation, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == (3 if batch else 2):
            raise RuntimeError("synthetic cleanup submission failure")
        return original(operation, *args, **kwargs)

    monkeypatch.setattr(broker._execution._executor, "submit", submit)
    result = asyncio.run(invoke(broker, batch))
    assert (result[0].value if batch else result) == 1
    assert closed.is_set()
    assert broker.resource_snapshot()["resource_usage"]["active_sessions"] == 0


def test_batch_guard_timeout_before_write_is_definite():
    broker = build_broker()
    called = []

    async def refuse(item):
        raise TimeoutError("synthetic ordering deadline")

    outcomes = asyncio.run(broker._execution._batch_client_call(
        "a", (1,), lambda client, item: called.append(item), max_items=1, write=True, before_item=refuse,
    ))
    assert called == []
    assert outcomes[0].error.code == "timeout"


def test_single_call_cancellation_retains_permit_through_loop_shutdown():
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class ClosingClient(Client):
        def __exit__(self, *args):
            closed.set()

    broker = build_broker(ClosingClient)

    def blocked(client):
        entered.set()
        assert release.wait(1)

    async def scenario():
        task = asyncio.create_task(broker._client_call("a", blocked))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert broker.resource_snapshot()["resource_usage"]["active_sessions"] == 1
        assert not closed.is_set()

    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert closed.wait(1)
    for _ in range(100):
        if broker.resource_snapshot()["resource_usage"]["active_sessions"] == 0:
            break
        time.sleep(0.01)
    assert broker.resource_snapshot()["resource_usage"]["active_sessions"] == 0
    assert asyncio.run(broker._client_call("a", lambda client: "available")) == "available"
