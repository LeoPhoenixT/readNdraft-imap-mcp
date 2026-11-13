from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest

from readndraft_imap_mcp.broker import AccountConfig, AccountRegistry, BrokerService
from readndraft_imap_mcp.broker.credential_work import CredentialWorkPool
from readndraft_imap_mcp.broker.errors import RequestQuotaError
from readndraft_imap_mcp.broker.limits import AccountRequestQuota
from readndraft_imap_mcp.broker.settings import DEFAULT_CAPACITY


class Client:
    def __init__(self, account, secret):
        assert secret == "synthetic"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@pytest.mark.parametrize("legacy_async", [False, True])
def test_abandoned_credential_worker_keeps_capacity_and_skips_cancelled_queue(legacy_async):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    loads = []

    def blocking_load(account_id):
        loads.append(account_id)
        entered.set()
        assert release.wait(5)
        return "synthetic"

    class SyncCredentials:
        load_secret_sync = staticmethod(blocking_load)

    class AsyncCredentials:
        async def load_secret(self, account_id):
            return await asyncio.to_thread(blocking_load, account_id)

    broker = BrokerService(
        AccountRegistry([AccountConfig(key, f"{key}.invalid", 993, key) for key in ("a", "b", "c")]),
        AsyncCredentials() if legacy_async else SyncCredentials(),
        Client,
        capacity_settings=replace(DEFAULT_CAPACITY, credential_workers=1, waiting_credential_work=1),
    )

    async def scenario():
        first = asyncio.create_task(broker._client_call("a", lambda client: None))
        assert await asyncio.to_thread(entered.wait, 5)
        queued = asyncio.create_task(broker._client_call("b", lambda client: None))
        # Credential queue insertion occurs synchronously before its first await.
        await asyncio.sleep(0)
        queued.cancel()
        first.cancel()
        for task in (queued, first):
            with pytest.raises(asyncio.CancelledError):
                await task
        usage = broker.resource_snapshot()["resource_usage"]
        assert usage["credential_running"] == 1
        assert usage["credential_queued"] == 1
        assert usage["active_sessions"] == 2
        with pytest.raises(RequestQuotaError) as rejected:
            await broker._client_call("c", lambda client: None)
        assert rejected.value.reason == "credential_capacity"
        assert loads == ["a"]

    try:
        # Caller event-loop shutdown must not wait on, cancel or hide the work.
        asyncio.run(scenario())
        assert broker.resource_snapshot()["resource_usage"]["active_sessions"] == 2
    finally:
        release.set()
        broker.close()
        finished.set()
    assert finished.is_set()
    assert loads == ["a"]
    usage = broker.resource_snapshot()["resource_usage"]
    assert usage["active_sessions"] == usage["credential_running"] == usage["credential_queued"] == 0


def test_repeated_cancelled_requests_do_not_spawn_more_credential_work():
    entered = threading.Event()
    release = threading.Event()
    loads = 0

    class Credentials:
        def load_secret_sync(self, account_id):
            nonlocal loads
            loads += 1
            entered.set()
            assert release.wait(5)
            return "synthetic"

    broker = BrokerService(
        AccountRegistry([AccountConfig("a", "a.invalid", 993, "a")]),
        Credentials(),
        Client,
        max_imap_workers=1,
        max_waiting_imap_work=4,
        quota=AccountRequestQuota(max_concurrent=1),
    )

    async def scenario():
        first = asyncio.create_task(broker._client_call("a", lambda client: None))
        assert await asyncio.to_thread(entered.wait, 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        for _ in range(3):
            following = asyncio.create_task(broker._client_call("a", lambda client: None))
            await asyncio.sleep(0)
            following.cancel()
            with pytest.raises(asyncio.CancelledError):
                await following
        assert loads == 1
        assert broker.resource_snapshot()["resource_usage"]["credential_running"] == 1
        assert broker.resource_snapshot()["resource_usage"]["active_sessions"] == 1

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        broker.close()
    assert loads == 1


def test_pool_skips_expired_work_and_handles_submission_failure(monkeypatch):
    pool = CredentialWorkPool(1, 0)
    called = []

    def expired():
        raise TimeoutError("synthetic expired queue entry")

    with pytest.raises(TimeoutError):
        pool.submit(expired, lambda: called.append(1)).result(timeout=5)
    assert called == []
    assert pool.usage()["credential_running"] == 0

    def reject(*args):
        raise RuntimeError("synthetic executor rejection")

    monkeypatch.setattr(pool._executor, "submit", reject)
    with pytest.raises(RuntimeError, match="executor rejection"):
        pool.submit(lambda: None, lambda: None)
    assert pool.usage()["credential_queued"] == 0
    monkeypatch.undo()
    assert pool.submit(lambda: None, lambda: 42).result(timeout=5) == 42
    pool.close()
    with pytest.raises(RuntimeError, match="closed"):
        pool.submit(lambda: None, lambda: None)


def test_credential_timeout_retains_physical_ownership_until_backend_returns(monkeypatch):
    from time import monotonic

    entered = threading.Event()
    release = threading.Event()

    class Credentials:
        def load_secret_sync(self, account_id):
            entered.set()
            assert release.wait(5)
            return "synthetic"

    broker = BrokerService(
        AccountRegistry([AccountConfig("a", "a.invalid", 993, "a")]),
        Credentials(),
        Client,
        max_imap_workers=1,
        max_waiting_imap_work=0,
    )
    pool = broker._execution._credential_pool
    original = pool.submit

    def expire_after_physical_start(check, operation):
        submitted = original(check, operation)
        assert entered.wait(5)
        # Expire at the worker-entry barrier, independent of thread startup time.
        broker._execution._context().deadline = monotonic() - 1
        return submitted

    monkeypatch.setattr(pool, "submit", expire_after_physical_start)
    try:
        with pytest.raises(TimeoutError):
            asyncio.run(broker._client_call("a", lambda client: None))
        usage = broker.resource_snapshot()["resource_usage"]
        assert usage["credential_running"] == usage["active_sessions"] == 1
        with pytest.raises(RequestQuotaError) as rejected:
            asyncio.run(broker._client_call("a", lambda client: None))
        assert rejected.value.reason == "imap_worker_capacity"
    finally:
        release.set()
        broker.close()
    assert broker.resource_snapshot()["resource_usage"]["active_sessions"] == 0


def test_shutdown_skips_queued_credentials_and_drains_owned_work():
    entered = threading.Event()
    release = threading.Event()
    loads = []

    class Credentials:
        def load_secret_sync(self, account_id):
            loads.append(account_id)
            entered.set()
            assert release.wait(5)
            return "synthetic"

    broker = BrokerService(
        AccountRegistry([AccountConfig(key, f"{key}.invalid", 993, key) for key in ("a", "b")]),
        Credentials(),
        Client,
        capacity_settings=replace(DEFAULT_CAPACITY, credential_workers=1, waiting_credential_work=1),
    )

    async def scenario():
        first = asyncio.create_task(broker._client_call("a", lambda client: None))
        assert await asyncio.to_thread(entered.wait, 5)
        second = asyncio.create_task(broker._client_call("b", lambda client: None))
        await asyncio.sleep(0)
        assert broker.resource_snapshot()["resource_usage"]["credential_queued"] == 1
        broker.close(wait=False)
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, RuntimeError) for result in results)
        with pytest.raises(RuntimeError, match="closed"):
            await broker._client_call("a", lambda client: None)

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        broker.close()
    assert loads == ["a"]
    usage = broker.resource_snapshot()["resource_usage"]
    assert usage["credential_running"] == usage["credential_queued"] == usage["active_sessions"] == 0
