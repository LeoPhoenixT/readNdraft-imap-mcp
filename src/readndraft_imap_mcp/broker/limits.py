from __future__ import annotations

import threading
from collections import defaultdict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from time import monotonic


class RequestQuotaError(RuntimeError):
    """Raised when an account exceeds its local request budget."""


class AccountRequestQuota:
    """Thread-safe per-account concurrency and sliding-window quota."""

    def __init__(
        self,
        *,
        max_concurrent: int = 2,
        requests_per_minute: int = 120,
        clock=monotonic,
    ) -> None:
        if max_concurrent < 1 or requests_per_minute < 1:
            raise ValueError("request quota values must be positive")
        self._max_concurrent = max_concurrent
        self._requests_per_minute = requests_per_minute
        self._clock = clock
        self._lock = threading.Lock()
        self._active: dict[str, int] = defaultdict(int)
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    @contextmanager
    def slot(self, account_id: str, *, cost: int = 1) -> Iterator[None]:
        if cost < 1:
            raise ValueError("request quota cost must be positive")
        now = self._clock()
        with self._lock:
            history = self._requests[account_id]
            while history and history[0] <= now - 60:
                history.popleft()
            if len(history) + cost > self._requests_per_minute:
                raise RequestQuotaError("account request rate limit exceeded")
            if self._active[account_id] >= self._max_concurrent:
                raise RequestQuotaError("account concurrency limit exceeded")
            history.extend(now for _ in range(cost))
            self._active[account_id] += 1
        try:
            yield
        finally:
            with self._lock:
                self._active[account_id] -= 1
