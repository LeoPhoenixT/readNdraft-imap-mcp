"""Synthetic resource loads: these budgets describe local memory/work, not IMAP provider limits."""

from __future__ import annotations

import asyncio
from collections import Counter
from time import monotonic

import pytest

from readndraft_imap_mcp.broker.errors import RequestQuotaError
from readndraft_imap_mcp.broker.scheduler import WorkScheduler
from readndraft_imap_mcp.broker.settings import DEFAULT_CAPACITY


def test_default_hot_account_load_leaves_workers_for_idle_accounts():
    async def scenario():
        settings = DEFAULT_CAPACITY
        scheduler = WorkScheduler(
            workers=settings.imap_workers,
            account_sessions=settings.account_sessions,
            waiting=settings.waiting_imap_work,
            account_waiting=settings.waiting_account_work,
        )
        permits = [await scheduler.acquire("hot", monotonic() + 10) for _ in range(2)]
        waiting = [asyncio.create_task(scheduler.acquire("hot", monotonic() + 10)) for _ in range(22)]
        await asyncio.sleep(0)
        assert scheduler.usage()["scheduler_waiting"] == 4
        other = await scheduler.acquire("idle", monotonic() + 10)
        assert scheduler.usage()["scheduler_running"] == 3
        rejected = [task for task in waiting if task.done()]
        assert len(rejected) == 18
        for task in rejected:
            with pytest.raises(RequestQuotaError) as error:
                await task
            assert error.value.reason == "account_queue_capacity"
        for task in waiting:
            if not task.done():
                task.cancel()
        await asyncio.gather(*waiting, return_exceptions=True)
        other.release()
        for permit in permits:
            permit.release()
        assert scheduler.usage()["scheduler_running"] == 0
        assert scheduler.usage()["scheduler_waiting"] == 0

    asyncio.run(scenario())


def test_round_robin_across_accounts_and_fifo_within_each_account():
    async def scenario():
        scheduler = WorkScheduler(workers=1, account_sessions=1, waiting=16, account_waiting=4)
        first = await scheduler.acquire("a", monotonic() + 10)
        order = []

        async def run(key, index):
            permit = await scheduler.acquire(key, monotonic() + 10)
            order.append((key, index))
            assert scheduler.usage()["scheduler_running"] == 1
            permit.release()

        tasks = [asyncio.create_task(run(key, index)) for key in ("a", "b", "c") for index in range(3)]
        await asyncio.sleep(0)
        first.release()
        await asyncio.gather(*tasks)
        assert order == [(key, index) for index in range(3) for key in ("a", "b", "c")]

    asyncio.run(scenario())


def test_global_queue_bound_and_work_conservation_under_synthetic_load():
    async def scenario():
        scheduler = WorkScheduler(workers=8, account_sessions=2, waiting=16, account_waiting=4)
        initial = [await scheduler.acquire(key, monotonic() + 10) for key in range(4) for _ in range(2)]
        gates = [asyncio.Event() for _ in range(16)]
        entered = asyncio.Queue()
        active = Counter()

        async def run(index):
            key = index % 8
            permit = await scheduler.acquire(key, monotonic() + 10)
            active[key] += 1
            assert active[key] <= 2
            assert scheduler.usage()["scheduler_running"] <= 8
            await entered.put(index)
            await gates[index].wait()
            active[key] -= 1
            permit.release()

        tasks = [asyncio.create_task(run(index)) for index in range(16)]
        await asyncio.sleep(0)
        assert scheduler.usage()["scheduler_waiting"] == 16
        with pytest.raises(RequestQuotaError) as rejected:
            await scheduler.acquire("overflow", monotonic() + 10)
        assert rejected.value.reason == "imap_worker_capacity"
        for permit in initial:
            permit.release()
        # All eight physical worker slots remain useful across eligible accounts.
        first_wave = [await entered.get() for _ in range(8)]
        assert scheduler.usage()["scheduler_running"] == 8
        for index in first_wave:
            gates[index].set()
        for _ in range(8):
            index = await entered.get()
            gates[index].set()
        await asyncio.gather(*tasks)
        assert scheduler.usage()["scheduler_waiting"] == 0
        assert scheduler.usage()["scheduler_running"] == 0

    asyncio.run(scenario())


def test_cancelled_expired_and_closed_waiters_never_start():
    async def scenario():
        scheduler = WorkScheduler(workers=1, account_sessions=1, waiting=4, account_waiting=4)
        first = await scheduler.acquire("a", monotonic() + 10)
        cancelled = asyncio.create_task(scheduler.acquire("a", monotonic() + 10))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        with pytest.raises(RequestQuotaError) as expired:
            await scheduler.acquire("a", monotonic() - 1)
        assert expired.value.reason == "session_queue_timeout"
        closing = asyncio.create_task(scheduler.acquire("b", monotonic() + 10))
        await asyncio.sleep(0)
        scheduler.close()
        with pytest.raises(RequestQuotaError):
            await closing
        first.release()
        first.release()  # Physical callbacks are safely idempotent.
        assert scheduler.usage()["scheduler_running"] == 0
        assert scheduler.usage()["scheduler_waiting"] == 0
        with pytest.raises(RuntimeError, match="closed"):
            await scheduler.acquire("a", monotonic() + 10)

    asyncio.run(scenario())


def test_cancel_after_grant_before_delivery_returns_both_resources():
    async def scenario():
        scheduler = WorkScheduler(workers=1, account_sessions=1, waiting=4, account_waiting=4)
        first = await scheduler.acquire("a", monotonic() + 10)
        queued = asyncio.create_task(scheduler.acquire("b", monotonic() + 10))
        await asyncio.sleep(0)
        first.release()
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert scheduler.usage()["scheduler_running"] == 0
        replacement = await scheduler.acquire("b", monotonic() + 10)
        replacement.release()

    asyncio.run(scenario())


def test_queued_expiry_uses_clock_barrier_and_never_obtains_a_grant():
    async def scenario():
        now = [100.0]
        scheduler = WorkScheduler(workers=1, account_sessions=1, waiting=4, account_waiting=4, clock=lambda: now[0])
        first = await scheduler.acquire("a", 200.0)
        expired = asyncio.create_task(scheduler.acquire("b", 110.0))
        following = asyncio.create_task(scheduler.acquire("c", 200.0))
        await asyncio.sleep(0)
        now[0] = 111.0
        first.release()
        with pytest.raises(RequestQuotaError) as error:
            await expired
        assert error.value.reason == "session_queue_timeout"
        permit = await following
        assert scheduler.usage()["scheduler_running"] == 1
        assert scheduler.usage()["rejections"]["session_queue_timeout"] == 1
        permit.release()
        assert scheduler.usage()["scheduler_running"] == scheduler.usage()["scheduler_waiting"] == 0

    asyncio.run(scenario())
