from __future__ import annotations

import asyncio
from time import monotonic

import pytest

from readndraft_imap_mcp.broker import AccountRequestQuota, RequestQuotaError


def test_default_task_bucket_capacity_is_exactly_120() -> None:
    quota = AccountRequestQuota(clock=lambda: 100.0)
    for _ in range(120):
        quota.admit_task(("personal",))
    with pytest.raises(RequestQuotaError):
        quota.admit_task(("personal",))


def test_token_bucket_starts_full_and_refills_smoothly_at_exact_boundary() -> None:
    now = [100.0]
    quota = AccountRequestQuota(
        requests_per_minute=2,
        refill_per_second=2.0,
        clock=lambda: now[0],
    )

    quota.admit_task(("personal",))
    quota.admit_task(("personal",))
    with pytest.raises(RequestQuotaError) as rejected:
        quota.admit_task(("personal",))
    assert rejected.value.reason == "task_rate"
    assert rejected.value.retry_after_seconds == 1

    now[0] += 0.499
    with pytest.raises(RequestQuotaError):
        quota.admit_task(("personal",))
    now[0] += 0.001
    quota.admit_task(("personal",))


def test_multi_account_admission_is_atomic_and_deduplicates_accounts() -> None:
    now = [100.0]
    quota = AccountRequestQuota(requests_per_minute=2, refill_per_second=1.0, clock=lambda: now[0])

    quota.admit_task(("blocked", "blocked"))
    quota.admit_task(("blocked",))
    quota.admit_task(("available",))
    with pytest.raises(RequestQuotaError):
        quota.admit_task(("blocked", "available"))

    # The failed atomic admission did not consume the remaining token.
    quota.admit_task(("available",))
    with pytest.raises(RequestQuotaError):
        quota.admit_task(("available",))


def test_session_permits_are_fifo() -> None:
    async def scenario() -> None:
        quota = AccountRequestQuota(max_concurrent=1)
        first = await quota.acquire_session("personal", monotonic() + 1)
        order: list[int] = []

        async def wait(index: int) -> None:
            permit = await quota.acquire_session("personal", monotonic() + 1)
            order.append(index)
            permit.release()

        second = asyncio.create_task(wait(2))
        await asyncio.sleep(0)
        third = asyncio.create_task(wait(3))
        await asyncio.sleep(0)
        first.release()
        await asyncio.gather(second, third)
        assert order == [2, 3]

    asyncio.run(scenario())


def test_cancelled_session_waiter_does_not_block_the_queue() -> None:
    async def scenario() -> None:
        quota = AccountRequestQuota(max_concurrent=1)
        first = await quota.acquire_session("personal", monotonic() + 1)
        cancelled = asyncio.create_task(quota.acquire_session("personal", monotonic() + 1))
        await asyncio.sleep(0)
        following = asyncio.create_task(quota.acquire_session("personal", monotonic() + 1))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        first.release()
        permit = await following
        permit.release()
        assert quota.usage()["active_sessions"] == 0
        assert quota.usage()["queued_session_requests"] == 0

    asyncio.run(scenario())


def test_session_queue_deadline_is_structured_and_releases_after_exception() -> None:
    async def scenario() -> None:
        quota = AccountRequestQuota(max_concurrent=1)
        first = await quota.acquire_session("personal", monotonic() + 1)
        with pytest.raises(RequestQuotaError) as rejected:
            await quota.acquire_session("personal", monotonic() + 0.01)
        assert rejected.value.code == "timeout"
        assert rejected.value.reason == "session_queue_timeout"
        first.release()
        replacement = await quota.acquire_session("personal", monotonic() + 1)
        replacement.release()
        assert quota.usage()["rejections"]["session_queue_timeout"] == 1

    asyncio.run(scenario())
