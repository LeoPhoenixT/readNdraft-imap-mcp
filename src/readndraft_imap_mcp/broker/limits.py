from __future__ import annotations

import math
import threading
from collections import defaultdict
from time import monotonic
from typing import Hashable

from .errors import RequestQuotaError as RequestQuotaError
from .scheduler import SessionPermit, WorkScheduler
from .settings import DEFAULT_CAPACITY

PhysicalAccountKey = tuple[str, int, str]


class AccountRequestQuota:
    """Thread-safe task token buckets and fair per-account session permits."""

    def __init__(
        self,
        *,
        max_concurrent: int = DEFAULT_CAPACITY.account_sessions,
        task_bucket_capacity: int | None = None,
        requests_per_minute: int | None = None,
        refill_per_second: float = DEFAULT_CAPACITY.task_refill_per_second,
        clock=monotonic,
    ) -> None:
        if task_bucket_capacity is not None and requests_per_minute is not None:
            raise ValueError("use task_bucket_capacity or legacy requests_per_minute, not both")
        capacity = task_bucket_capacity if task_bucket_capacity is not None else requests_per_minute
        capacity = DEFAULT_CAPACITY.task_bucket_capacity if capacity is None else capacity
        if max_concurrent < 1 or capacity < 1 or refill_per_second <= 0:
            raise ValueError("request quota values must be positive")
        self._max_concurrent = max_concurrent
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[Hashable, tuple[float, float]] = {}
        self._rejections: dict[str, int] = defaultdict(int)
        self._scheduler = WorkScheduler(workers=DEFAULT_CAPACITY.imap_workers,
                                        account_sessions=max_concurrent,
                                        waiting=DEFAULT_CAPACITY.waiting_imap_work,
                                        account_waiting=DEFAULT_CAPACITY.waiting_account_work)

    def configure_scheduler(self, *, workers: int, waiting: int, account_waiting: int) -> None:
        # A grant reserves one bounded executor position for the whole client
        # lifecycle (including interleaved batches). Workers remain independently
        # bounded by the physical executor. Account-blocked waiters and reserved
        # executor positions share one total admission budget.
        self._scheduler = WorkScheduler(workers=workers + waiting, account_sessions=self._max_concurrent,
                                        waiting=waiting, account_waiting=account_waiting,
                                        total_capacity=workers + waiting)

    def admit_task(self, account_keys: tuple[Hashable, ...]) -> None:
        """Atomically consume one task token from every distinct account."""
        keys = tuple(dict.fromkeys(account_keys))
        if not keys:
            return
        now = self._clock()
        with self._lock:
            available: dict[Hashable, float] = {}
            retry_after = 1
            for key in keys:
                tokens, updated = self._buckets.get(key, (float(self._capacity), now))
                tokens = min(float(self._capacity), tokens + max(0.0, now - updated) * self._refill_per_second)
                available[key] = tokens
                if tokens < 1.0:
                    retry_after = max(
                        retry_after,
                        math.ceil((1.0 - tokens) / self._refill_per_second),
                    )
            if any(tokens < 1.0 for tokens in available.values()):
                self._rejections["task_rate"] += 1
                raise RequestQuotaError(
                    "account task rate limit exceeded",
                    reason="task_rate",
                    retry_after_seconds=retry_after,
                )
            for key, tokens in available.items():
                self._buckets[key] = (tokens - 1.0, now)

    async def acquire_session(self, account_key: Hashable, deadline: float) -> SessionPermit:
        """Compatibility adapter; execution scheduling lives in scheduler.py."""
        return await self._scheduler.acquire(account_key, deadline)

    def close(self) -> None:
        self._scheduler.close()

    def record_rejection(self, reason: str) -> None:
        with self._lock:
            self._rejections[reason] += 1

    def limits(self) -> dict[str, int | float]:
        return {
            "task_bucket_capacity": self._capacity,
            "task_refill_per_second": self._refill_per_second,
            "account_sessions": self._max_concurrent,
        }

    def usage(self) -> dict[str, object]:
        usage = self._scheduler.usage()
        with self._lock:
            recorded = dict(self._rejections)
        scheduler_rejections = usage["rejections"]
        usage["rejections"] = {
            reason: recorded.get(reason, 0) + scheduler_rejections.get(reason, 0)
            for reason in ("task_rate", "session_queue_timeout", "imap_worker_capacity", "account_queue_capacity")
        }
        return usage
