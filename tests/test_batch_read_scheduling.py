from __future__ import annotations

import asyncio
import threading
import time

import pytest

from readndraft_imap_mcp.broker import AccountConfig, AccountRegistry, BrokerService
from readndraft_imap_mcp.imap.models import MessageContent, MessageIdentity


class Credentials:
    async def load_secret(self, account_id):
        return "synthetic-test-value"


class Client:
    created: list[str]

    def __init__(self, account, secret):
        self.account = account

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get_message_budgeted(self, identity, reserve_source):
        assert reserve_source(4)
        return MessageContent(identity, {}, identity.uid, (), (), 4)


def broker_for(client=Client, credentials=None, **kwargs):
    registry = AccountRegistry([
        AccountConfig(name, name + ".invalid", 993, name + "@example.invalid") for name in ("a", "b")
    ])
    return BrokerService(registry, credentials or Credentials(), client, **kwargs)


def identities(*accounts):
    return tuple(MessageIdentity(name, "INBOX", "1", str(i + 1)) for i, name in enumerate(accounts))


def assert_released(broker):
    usage = broker.resource_snapshot()["resource_usage"]
    assert usage["active_sessions"] == 0
    assert usage["queued_session_requests"] == 0
    assert asyncio.run(broker._client_call("b", lambda client: "available")) == "available"


@pytest.mark.parametrize("stage", ["credentials", "entry"])
def test_failed_account_releases_all_unstarted_budget_positions(stage):
    class FailingCredentials(Credentials):
        async def load_secret(self, account_id):
            if stage == "credentials" and account_id == "a":
                raise KeyError("synthetic missing credential")
            return await super().load_secret(account_id)

    class FailingClient(Client):
        def __enter__(self):
            if stage == "entry" and self.account.account_id == "a":
                raise OSError("synthetic connection failure")
            return self

    broker = broker_for(FailingClient, FailingCredentials(), request_timeout_seconds=0.5)
    results = asyncio.run(broker.get_emails(identities("a", "b", "a", "b")))
    assert [item.ok for item in results] == [False, True, False, True]
    assert [item.message.text for item in results if item.ok] == ["2", "4"]
    assert_released(broker)


@pytest.mark.parametrize("delay", [0, 0.05])
def test_interleaved_accounts_progress_with_one_worker_and_reuse_sessions(delay):
    created = []

    class DelayedCredentials(Credentials):
        async def load_secret(self, account_id):
            if account_id == "b":
                await asyncio.sleep(delay)
            return await super().load_secret(account_id)

    class CountingClient(Client):
        def __init__(self, account, secret):
            super().__init__(account, secret)
            created.append(account.account_id)

    broker = broker_for(CountingClient, DelayedCredentials(), max_imap_workers=1, request_timeout_seconds=0.5)
    results = asyncio.run(broker.get_emails(identities("a", "b", "a")))
    assert [item.ok for item in results] == [True, True, True]
    assert [item.message.text for item in results] == ["1", "2", "3"]
    assert sorted(created) == ["a", "b"]
    assert_released(broker)


def test_deadline_preserves_completed_read_and_releases_waiting_accounts():
    class SlowCredentials(Credentials):
        async def load_secret(self, account_id):
            if account_id == "b":
                await asyncio.sleep(1)
            return await super().load_secret(account_id)

    broker = broker_for(credentials=SlowCredentials(), request_timeout_seconds=0.15)
    results = asyncio.run(broker.get_emails(identities("a", "b", "a")))
    assert [item.ok for item in results] == [True, False, False]
    assert results[0].message.text == "1"
    assert [item.error.code for item in results[1:]] == ["timeout", "timeout"]
    assert broker.resource_snapshot()["resource_usage"]["active_sessions"] == 0


def test_cancellation_releases_ordering_waits_and_worker_capacity():
    reserved = threading.Event()
    release = threading.Event()

    class BlockingClient(Client):
        def get_message_budgeted(self, identity, reserve_source):
            assert reserve_source(4)
            if identity.account_id == "a":
                reserved.set()
                assert release.wait(1)
            return MessageContent(identity, {}, identity.uid, (), (), 4)

    broker = broker_for(BlockingClient, max_imap_workers=2, max_waiting_imap_work=0)

    async def scenario():
        task = asyncio.create_task(broker.get_emails(identities("a", "b", "a")))
        try:
            assert await asyncio.to_thread(reserved.wait, 1)
            await asyncio.sleep(0.03)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            for _ in range(100):
                if broker.resource_snapshot()["resource_usage"]["active_sessions"] == 0:
                    break
                await asyncio.sleep(0.01)
        finally:
            release.set()

    asyncio.run(scenario())
    assert_released(broker)


def test_worker_settlement_wait_obeys_deadline():
    class SlowClient(Client):
        def get_message_budgeted(self, identity, reserve_source):
            assert reserve_source(4)
            if identity.account_id == "a":
                time.sleep(0.12)
            return MessageContent(identity, {}, identity.uid, (), (), 4)

    broker = broker_for(SlowClient, request_timeout_seconds=0.06)
    results = asyncio.run(broker.get_emails(identities("a", "b")))
    assert results[0].ok
    assert not results[1].ok and results[1].error.code == "timeout"
    assert_released(broker)


def test_repeated_cancellation_and_loop_shutdown_leave_cleanup_owned_by_worker():
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()

    class BlockingClient(Client):
        def __exit__(self, *args):
            exited.set()

    broker = broker_for(BlockingClient, max_imap_workers=1, max_waiting_imap_work=0)

    def blocked(client, item):
        entered.set()
        assert release.wait(1)
        return item

    async def scenario():
        task = asyncio.create_task(broker._batch_client_call("a", (1,), blocked, max_items=1))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert broker.resource_snapshot()["resource_usage"]["active_sessions"] == 1
        assert not exited.is_set()

    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert exited.wait(1)
    for _ in range(100):
        if broker.resource_snapshot()["resource_usage"]["active_sessions"] == 0:
            break
        time.sleep(0.01)
    assert_released(broker)
