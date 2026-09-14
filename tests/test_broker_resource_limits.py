from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from readndraft_imap_mcp.broker.accounts import AccountConfig, AccountRegistry
from readndraft_imap_mcp.broker.limits import AccountRequestQuota, RequestQuotaError
from readndraft_imap_mcp.broker.service import BrokerService
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
    broker = BrokerService(
        _registry(AccountConfig("mail", "imap.example.com", 993, "same@example.com")),
        Credentials(),
        Client,
        quota=quota,
        request_timeout_seconds=0.02,
    )

    with pytest.raises(TimeoutError):
        asyncio.run(broker._client_call("mail", lambda client: time.sleep(0.1)))
    assert quota.usage()["active_sessions"] == 1
    time.sleep(0.12)
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
    broker = BrokerService(
        _registry(AccountConfig("mail", "imap.example.com", 993, "same@example.com")),
        Credentials(),
        Client,
        request_timeout_seconds=0.01,
    )

    def write(client, item):
        if item == 2:
            time.sleep(0.02)
            raise TimeoutError("private deadline detail")
        return item

    outcomes = asyncio.run(
        broker._batch_client_call(
            "mail",
            (1, 2, 3),
            write,
            max_items=3,
            response_timeout=False,
            write=True,
        )
    )
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
    assert snapshot["resource_limits"] == {
        "task_bucket_capacity": 120,
        "task_refill_per_second": 2.0,
        "account_sessions": 2,
        "imap_workers": 8,
        "waiting_imap_work": 16,
    }
    encoded = repr(snapshot)
    assert "private-alias" not in encoded
    assert "secret.example.com" not in encoded
    assert "owner@example.com" not in encoded
