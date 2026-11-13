"""Bounded credential execution whose ownership outlives response deadlines."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TypeVar

from .errors import RequestQuotaError

T = TypeVar("T")


class CredentialWorkPool:
    def __init__(self, workers: int, waiting: int) -> None:
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="readndraft-credential")
        self._capacity = threading.BoundedSemaphore(workers + waiting)
        self._lock = threading.Lock()
        self._running = 0
        self._waiting = 0
        self._closed = False
        self._rejections = 0

    def submit(self, check: Callable[[], None], operation: Callable[[], T]) -> Future[T]:
        with self._lock:
            if self._closed:
                raise RuntimeError("credential executor is closed")
            if not self._capacity.acquire(blocking=False):
                self._rejections += 1
                raise RequestQuotaError("broker credential capacity exceeded", reason="credential_capacity")
            self._waiting += 1

            def run() -> T:
                with self._lock:
                    self._waiting -= 1
                    self._running += 1
                try:
                    check()  # Never touch credentials for expired/cancelled queued work.
                    return operation()
                finally:
                    with self._lock:
                        self._running -= 1
                    self._capacity.release()

            try:
                # Never cancel this Future: even abandoned queue entries keep
                # capacity until physically drained, bounding executor storage.
                return self._executor.submit(run)
            except BaseException:
                self._waiting -= 1
                self._capacity.release()
                raise

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait)

    def usage(self) -> dict[str, int]:
        with self._lock:
            return {
                "credential_running": self._running,
                "credential_queued": self._waiting,
                "credential_capacity": self._rejections,
            }
