from __future__ import annotations

import asyncio
import threading
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from readndraft_imap_mcp.broker.accounts import AccountConfig, AccountRegistry
from readndraft_imap_mcp.broker.limits import AccountRequestQuota, RequestQuotaError
from readndraft_imap_mcp.broker.service import BrokerService
from readndraft_imap_mcp.broker.settings import DEFAULT_CAPACITY
from readndraft_imap_mcp.imap.models import DraftCreationResult, MessageIdentity


class Credentials:
    def __init__(self) -> None:
        self.loads = 0

    async def load_secret(self, account_id: str) -> str:
        self.loads += 1
        return "broker-only-secret"


class Client:
    def __init__(self, account, secret) -> None:
        self.account = account

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _registry(*accounts: AccountConfig) -> AccountRegistry:
    return AccountRegistry(accounts)


def test_batch_of_fifty_consumes_one_top_level_task_token() -> None:
    quota = AccountRequestQuota(requests_per_minute=1, refill_per_second=0.000001)
    credentials = Credentials()
    broker = BrokerService(
        _registry(AccountConfig("mail", "imap.example.com", 993, "same@example.com")),
        credentials,
        Client,
        quota=quota,
    )

    outcomes = asyncio.run(
        broker._batch_client_call("mail", tuple(range(50)), lambda client, item: item, max_items=50)
    )
    assert tuple(item.value for item in outcomes) == tuple(range(50))
    with pytest.raises(RequestQuotaError) as rejected:
        asyncio.run(broker._client_call("mail", lambda client: None))
    assert rejected.value.reason == "task_rate"
    assert credentials.loads == 1


def test_aliases_for_same_physical_account_share_task_bucket() -> None:
    quota = AccountRequestQuota(requests_per_minute=1, refill_per_second=0.000001)
    credentials = Credentials()
    broker = BrokerService(
        _registry(
            AccountConfig("first", "IMAP.Example.com.", 993, "exact@example.com"),
            AccountConfig("alias", "imap.example.com", 993, "exact@example.com"),
            AccountConfig("other-user", "imap.example.com", 993, "Exact@example.com"),
        ),
        credentials,
        Client,
        quota=quota,
    )

    asyncio.run(broker._client_call("first", lambda client: None))
    with pytest.raises(RequestQuotaError):
        asyncio.run(broker._client_call("alias", lambda client: None))
    asyncio.run(broker._client_call("other-user", lambda client: None))
    assert credentials.loads == 2


def test_aliases_for_same_physical_account_share_fifo_session_limit() -> None:
    async def scenario() -> None:
        entered = threading.Event()
        release = threading.Event()
        created = 0

        class BlockingClient(Client):
            def __init__(self, account, secret) -> None:
                nonlocal created
                super().__init__(account, secret)
                created += 1

        quota = AccountRequestQuota(max_concurrent=1)
        broker = BrokerService(
            _registry(
                AccountConfig("first", "IMAP.Example.com.", 993, "exact@example.com"),
                AccountConfig("alias", "imap.example.com", 993, "exact@example.com"),
            ),
            Credentials(),
            BlockingClient,
            quota=quota,
        )

        def block(client):
            entered.set()
            release.wait(1)

        first = asyncio.create_task(broker._client_call("first", block))
        await asyncio.to_thread(entered.wait, 1)
        second = asyncio.create_task(broker._client_call("alias", lambda client: None))
        await asyncio.sleep(0.02)
        assert created == 1
        assert quota.usage()["queued_session_requests"] == 1
        release.set()
        await asyncio.gather(first, second)
        assert created == 2

    asyncio.run(scenario())


def test_multi_stage_reply_draft_consumes_only_one_task_token() -> None:
    class DraftClient(Client):
        def get_threading_headers(self, identity):
            return "<source@example.com>", None

        def append_draft(self, raw, message_id, attachment_hashes):
            return DraftCreationResult(
                self.account.account_id,
                "Drafts",
                "1",
                "2",
                message_id,
                attachment_hashes,
            )

    class Audit:
        async def record(self, event):
            return None

    class DraftStore:
        def create(self, **kwargs):
            return SimpleNamespace(draft_id="d" * 32)

    broker = BrokerService(
        _registry(AccountConfig("mail", "imap.example.com", 993, "same@example.com")),
        Credentials(),
        DraftClient,
        quota=AccountRequestQuota(requests_per_minute=1, refill_per_second=0.000001),
        audit=Audit(),
        drafts=DraftStore(),
    )
    result = asyncio.run(
        broker.create_draft(
            "mail",
            to=("recipient@example.com",),
            subject="Re: test",
            body="body",
            reply_to_message=MessageIdentity("mail", "INBOX", "1", "1"),
        )
    )
    assert result.draft_id == "d" * 32


def test_invalid_batch_is_rejected_before_admission_or_credentials() -> None:
    class CountingQuota(AccountRequestQuota):
        admissions = 0

        def admit_task(self, account_keys):
            self.admissions += 1
            return super().admit_task(account_keys)

    quota = CountingQuota()
    credentials = Credentials()
    broker = BrokerService(
        _registry(AccountConfig("mail", "imap.example.com", 993, "same@example.com")),
        credentials,
        Client,
        quota=quota,
    )

    with pytest.raises(ValueError, match="between 1 and 10"):
        asyncio.run(broker.get_emails(()))
    assert quota.admissions == 0
    assert credentials.loads == 0


def test_invalid_reply_draft_is_rejected_before_threading_admission() -> None:
    class CountingQuota(AccountRequestQuota):
        admissions = 0

        def admit_task(self, account_keys):
            self.admissions += 1
            return super().admit_task(account_keys)

    class Audit:
        async def record(self, event):
            return None

    quota = CountingQuota()
    credentials = Credentials()
    broker = BrokerService(
        _registry(AccountConfig("mail", "imap.example.com", 993, "same@example.com")),
        credentials,
        Client,
        quota=quota,
        audit=Audit(),
        drafts=SimpleNamespace(),
    )
    with pytest.raises(ValueError, match="invalid To address"):
        asyncio.run(
            broker.create_draft(
                "mail",
                to=("not-an-address",),
                subject="Re: test",
                body="body",
                reply_to_message=MessageIdentity("mail", "INBOX", "1", "1"),
            )
        )
    assert quota.admissions == 0
    assert credentials.loads == 0


def test_timed_out_worker_holds_session_permit_until_it_really_finishes() -> None:
    quota = AccountRequestQuota(max_concurrent=1)
    entered = threading.Event()
    release = threading.Event()
    broker = BrokerService(
        _registry(AccountConfig("mail", "imap.example.com", 993, "same@example.com")),
        Credentials(),
        Client,
        quota=quota,
        request_timeout_seconds=1,
    )

    def block(client):
        entered.set()
        assert release.wait(5)

    try:
        with pytest.raises(TimeoutError):
            asyncio.run(broker._client_call("mail", block))
        assert entered.is_set()
        assert quota.usage()["active_sessions"] == 1
    finally:
        release.set()
        broker.close()
    assert quota.usage()["active_sessions"] == 0


def test_worker_exception_releases_session_and_global_capacity() -> None:
    quota = AccountRequestQuota(max_concurrent=1)
    broker = BrokerService(
        _registry(AccountConfig("mail", "imap.example.com", 993, "same@example.com")),
        Credentials(),
        Client,
        quota=quota,
        max_imap_workers=1,
        max_waiting_imap_work=0,
    )
    with pytest.raises(RuntimeError, match="worker failed"):
        asyncio.run(
            broker._client_call(
                "mail", lambda client: (_ for _ in ()).throw(RuntimeError("worker failed"))
            )
        )
    assert quota.usage()["active_sessions"] == 0
    assert asyncio.run(broker._client_call("mail", lambda client: "ok")) == "ok"


def test_write_batch_preserves_success_and_marks_only_uncertain_item_unknown() -> None:
    called: list[int] = []
    broker = BrokerService(
        _registry(AccountConfig("mail", "imap.example.com", 993, "same@example.com")),
        Credentials(),
        Client,
    )

    def write(client, item):
        called.append(item)
        if item == 2:
            raise TimeoutError("private deadline detail")
        return item

    try:
        outcomes = asyncio.run(
            broker._batch_client_call(
                "mail",
                (1, 2, 3),
                write,
                max_items=3,
                write=True,
            )
        )
    finally:
        broker.close()
    assert called == [1, 2]
    assert outcomes[0].value == 1
    assert outcomes[1].error is not None and outcomes[1].error.code == "outcome_unknown"
    assert outcomes[2].error is not None and outcomes[2].error.code == "timeout"


def test_resource_snapshot_is_aggregate_and_contains_no_account_key_material() -> None:
    broker = BrokerService(
        _registry(AccountConfig("private-alias", "secret.example.com", 993, "owner@example.com")),
        Credentials(),
        Client,
    )
    asyncio.run(broker._client_call("private-alias", lambda client: None))

    snapshot = broker.resource_snapshot()
    assert snapshot["resource_limits"] == asdict(DEFAULT_CAPACITY)
    encoded = repr(snapshot)
    assert "private-alias" not in encoded
    assert "secret.example.com" not in encoded
    assert "owner@example.com" not in encoded


def test_resource_snapshot_reports_injected_quota_limits() -> None:
    broker = BrokerService(
        quota=AccountRequestQuota(
            max_concurrent=1,
            requests_per_minute=7,
            refill_per_second=0.5,
        ),
    )

    assert broker.resource_snapshot()["resource_limits"] == {
        **asdict(DEFAULT_CAPACITY),
        "task_bucket_capacity": 7,
        "task_refill_per_second": 0.5,
        "account_sessions": 1,
        "imap_workers": 8,
        "waiting_imap_work": 16,
    }


def test_default_hot_account_burst_preserves_an_idle_accounts_physical_progress():
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0
    credentials = Credentials()
    broker = BrokerService(
        _registry(AccountConfig("a", "a.invalid", 993, "a"), AccountConfig("b", "b.invalid", 993, "b")),
        credentials, Client,
    )

    def block(client):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
        try:
            assert release.wait(5)
        finally:
            with lock:
                active -= 1

    async def scenario():
        running = [asyncio.create_task(broker._client_call("a", block)) for _ in range(2)]
        assert await asyncio.to_thread(entered.wait, 5)
        burst = [asyncio.create_task(broker._client_call("a", lambda client: None)) for _ in range(22)]
        await asyncio.sleep(0)
        try:
            assert broker.resource_snapshot()["resource_usage"]["scheduler_waiting"] == 4
            assert await broker._client_call("b", lambda client: "progress") == "progress"
            rejected = [task for task in burst if task.done()]
            assert len(rejected) == 18
            for task in rejected:
                with pytest.raises(RequestQuotaError) as error:
                    await task
                assert error.value.reason == "account_queue_capacity"
            for task in burst:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*burst, return_exceptions=True)
            assert credentials.loads == 3  # Two hot sessions and one idle session.
        finally:
            release.set()
            await asyncio.gather(*running)
        assert broker.resource_snapshot()["resource_usage"]["active_sessions"] == 0

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        broker.close()
    assert peak == 2
