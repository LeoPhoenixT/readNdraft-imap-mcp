from __future__ import annotations

import asyncio
import math
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from time import monotonic
from typing import Hashable

PhysicalAccountKey = tuple[str, int, str]


class RequestQuotaError(RuntimeError):
    """Raised when an account exceeds its local request budget."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "rate_limited",
        reason: str = "task_rate",
        retry_after_seconds: int | None = None,
    ) -> None:
        self.code = code
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


@dataclass(slots=True)
class _Waiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[None]
    cancelled: bool = False
    granted: bool = False
    released: bool = False


class SessionPermit:
    def __init__(self, quota: AccountRequestQuota, account_key: Hashable) -> None:
        self._quota = quota
        self._account_key = account_key
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._quota._release_session(self._account_key)


class AccountRequestQuota:
    """Thread-safe task token buckets and fair per-account session permits."""

    def __init__(
        self,
        *,
        max_concurrent: int = 2,
        requests_per_minute: int = 120,
        refill_per_second: float = 2.0,
        clock=monotonic,
    ) -> None:
        if max_concurrent < 1 or requests_per_minute < 1 or refill_per_second <= 0:
            raise ValueError("request quota values must be positive")
        self._max_concurrent = max_concurrent
        self._capacity = requests_per_minute
        self._refill_per_second = refill_per_second
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[Hashable, tuple[float, float]] = {}
        self._active: dict[Hashable, int] = defaultdict(int)
        self._waiters: dict[Hashable, deque[_Waiter]] = defaultdict(deque)
        self._queued = 0
        self._rejections: dict[str, int] = defaultdict(int)

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
        loop = asyncio.get_running_loop()
        waiter: _Waiter | None = None
        with self._lock:
            queue = self._waiters[account_key]
            if self._active[account_key] < self._max_concurrent and not queue:
                self._active[account_key] += 1
                return SessionPermit(self, account_key)
            waiter = _Waiter(loop, loop.create_future())
            queue.append(waiter)
            self._queued += 1

        remaining = deadline - monotonic()
        try:
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(asyncio.shield(waiter.future), remaining)
            return SessionPermit(self, account_key)
        except asyncio.CancelledError:
            self._cancel_waiter(account_key, waiter, count_timeout=False)
            raise
        except TimeoutError:
            self._cancel_waiter(account_key, waiter, count_timeout=True)
            raise RequestQuotaError(
                "account session queue deadline expired",
                code="timeout",
                reason="session_queue_timeout",
            ) from None

    def _cancel_waiter(
        self, account_key: Hashable, waiter: _Waiter, *, count_timeout: bool
    ) -> None:
        release_grant = False
        with self._lock:
            waiter.cancelled = True
            if waiter.granted:
                if not waiter.released:
                    waiter.released = True
                    release_grant = True
            else:
                try:
                    self._waiters[account_key].remove(waiter)
                except ValueError:
                    pass
                else:
                    self._queued -= 1
            if count_timeout:
                self._rejections["session_queue_timeout"] += 1
        waiter.future.cancel()
        if release_grant:
            self._release_session(account_key)

    def _deliver(self, account_key: Hashable, waiter: _Waiter) -> None:
        release_grant = False
        with self._lock:
            if waiter.released:
                return
            if waiter.cancelled or waiter.future.cancelled():
                waiter.released = True
                release_grant = True
        if release_grant:
            self._release_session(account_key)
        elif not waiter.future.done():
            waiter.future.set_result(None)

    def _release_session(self, account_key: Hashable) -> None:
        selected: _Waiter | None = None
        with self._lock:
            if self._active[account_key] <= 0:
                raise RuntimeError("session permit released without an active session")
            self._active[account_key] -= 1
            queue = self._waiters[account_key]
            while queue:
                candidate = queue.popleft()
                self._queued -= 1
                if candidate.cancelled:
                    continue
                candidate.granted = True
                self._active[account_key] += 1
                selected = candidate
                break
        if selected is not None:
            selected.loop.call_soon_threadsafe(self._deliver, account_key, selected)

    def record_rejection(self, reason: str) -> None:
        with self._lock:
            self._rejections[reason] += 1

    def usage(self) -> dict[str, object]:
        with self._lock:
            return {
                "active_sessions": sum(self._active.values()),
                "queued_session_requests": self._queued,
                "rejections": {
                    reason: self._rejections.get(reason, 0)
                    for reason in (
                        "task_rate",
                        "session_queue_timeout",
                        "imap_worker_capacity",
                    )
                },
            }
