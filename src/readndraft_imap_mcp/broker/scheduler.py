"""Coordinated global/account grants with FIFO and round-robin queues."""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from time import monotonic
from typing import Hashable

from .errors import RequestQuotaError


@dataclass(slots=True)
class _Waiter:
    key: Hashable
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[None]
    deadline: float
    granted: bool = False
    released: bool = False


class SessionPermit:
    def __init__(self, scheduler: WorkScheduler, waiter: _Waiter) -> None:
        self._scheduler = scheduler
        self._waiter = waiter

    def release(self) -> None:
        self._scheduler.release(self._waiter)


class WorkScheduler:
    """No waiter holds one scarce permit while waiting for another."""

    def __init__(
        self,
        *,
        workers: int,
        account_sessions: int,
        waiting: int,
        account_waiting: int,
        total_capacity: int | None = None,
        clock=monotonic,
    ) -> None:
        self._clock = clock
        self._workers = workers
        self._total_capacity = workers + waiting if total_capacity is None else total_capacity
        self._account_sessions = account_sessions
        self._waiting_limit = waiting
        self._account_waiting = account_waiting
        self._lock = threading.Lock()
        self._active: dict[Hashable, int] = defaultdict(int)
        self._queues: dict[Hashable, deque[_Waiter]] = {}
        self._rotation: deque[Hashable] = deque()
        self._running = 0
        self._waiting = 0
        self._closed = False
        self._rejections: dict[str, int] = defaultdict(int)

    async def acquire(self, key: Hashable, deadline: float) -> SessionPermit:
        loop = asyncio.get_running_loop()
        waiter = _Waiter(key, loop, loop.create_future(), deadline)
        with self._lock:
            if self._closed:
                raise RuntimeError("broker scheduler is closed")
            if deadline <= self._clock():
                self._rejections["session_queue_timeout"] += 1
                raise self._timeout()
            immediate = self._running < self._workers and self._active[key] < self._account_sessions
            if immediate and not self._queues.get(key) and self._running + self._waiting < self._total_capacity:
                waiter.granted = True
                self._active[key] += 1
                self._running += 1
                return SessionPermit(self, waiter)
            if len(self._queues.get(key, ())) >= self._account_waiting:
                self._rejections["account_queue_capacity"] += 1
                raise RequestQuotaError("account work queue capacity exceeded", reason="account_queue_capacity")
            if self._waiting >= self._waiting_limit or self._running + self._waiting >= self._total_capacity:
                self._rejections["imap_worker_capacity"] += 1
                raise RequestQuotaError("broker work queue capacity exceeded", reason="imap_worker_capacity")
            if key not in self._queues:
                self._queues[key] = deque()
                self._rotation.append(key)
            self._queues[key].append(waiter)
            self._waiting += 1
        try:
            await asyncio.wait_for(asyncio.shield(waiter.future), max(0, deadline - self._clock()))
            return SessionPermit(self, waiter)
        except BaseException as exc:
            self.release(waiter)
            waiter.future.cancel()
            if isinstance(exc, TimeoutError):
                self.record_rejection("session_queue_timeout")
                raise self._timeout() from None
            raise

    @staticmethod
    def _timeout() -> RequestQuotaError:
        return RequestQuotaError("session queue deadline expired", code="timeout", reason="session_queue_timeout")

    def release(self, waiter: _Waiter) -> None:
        with self._lock:
            if waiter.released:
                return
            waiter.released = True
            if waiter.granted:
                self._running -= 1
                self._active[waiter.key] -= 1
                if not self._active[waiter.key]:
                    del self._active[waiter.key]
            else:
                queue = self._queues.get(waiter.key)
                if queue is not None:
                    try:
                        queue.remove(waiter)
                    except ValueError:
                        pass
                    else:
                        self._waiting -= 1
                    self._remove_empty(waiter.key)
            selected = self._dispatch()
        self._deliver(selected)

    def _remove_empty(self, key: Hashable) -> None:
        if key in self._queues and not self._queues[key]:
            del self._queues[key]
            self._rotation.remove(key)

    def _dispatch(self) -> list[_Waiter]:
        selected = []
        while not self._closed and self._running < self._workers and self._rotation:
            progressed = False
            for _ in range(len(self._rotation)):
                key = self._rotation[0]
                self._rotation.rotate(-1)
                queue = self._queues[key]
                # Expired work must never get a physical execution grant.
                while queue and (queue[0].released or queue[0].deadline <= self._clock()):
                    expired = queue.popleft()
                    self._waiting -= 1
                    self._rejections["session_queue_timeout"] += 1
                    try:
                        expired.loop.call_soon_threadsafe(self._expire, expired)
                    except RuntimeError:
                        pass
                if queue and self._active[key] < self._account_sessions:
                    waiter = queue.popleft()
                    self._waiting -= 1
                    waiter.granted = True
                    self._active[key] += 1
                    self._running += 1
                    selected.append(waiter)
                    progressed = True
                self._remove_empty(key)
                if self._running >= self._workers:
                    break
            if not progressed:
                break
        return selected

    @staticmethod
    def _expire(waiter: _Waiter) -> None:
        if not waiter.future.done():
            waiter.future.set_exception(WorkScheduler._timeout())

    def _deliver(self, selected: list[_Waiter]) -> None:
        for waiter in selected:
            try:
                waiter.loop.call_soon_threadsafe(self._grant, waiter)
            except RuntimeError:  # Caller loop already closed; return ownership.
                self.release(waiter)

    def _grant(self, waiter: _Waiter) -> None:
        if waiter.future.done() or waiter.released:
            self.release(waiter)
        else:
            waiter.future.set_result(None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            waiting = [waiter for queue in self._queues.values() for waiter in queue]
            self._queues.clear()
            self._rotation.clear()
            self._waiting = 0
        for waiter in waiting:
            waiter.released = True
            try:
                waiter.loop.call_soon_threadsafe(self._expire, waiter)
            except RuntimeError:
                pass

    def record_rejection(self, reason: str) -> None:
        with self._lock:
            self._rejections[reason] += 1

    def usage(self) -> dict[str, object]:
        with self._lock:
            return {
                "active_sessions": self._running,
                "queued_session_requests": self._waiting,
                "scheduler_running": self._running,
                "scheduler_waiting": self._waiting,
                "rejections": dict(self._rejections),
            }
