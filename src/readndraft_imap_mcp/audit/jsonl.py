from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .model import AuditEvent


@contextmanager
def _os_lock(path: Path) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            deadline = 100
            while True:
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    deadline -= 1
                    if deadline <= 0:
                        raise
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(descriptor)


class JsonlAuditSink:
    """Append safe audit events to a local file without logging message content."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("audit path must be absolute")
        if not path.parent.is_dir():
            raise ValueError("audit parent directory must already exist")
        self.path = path
        self._lock = threading.Lock()
        self._last_hash: str | None = None

    @property
    def _lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    @staticmethod
    def _hash(record: dict[str, object]) -> str:
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return hashlib.sha256(payload).hexdigest()

    def _verify_sync(self) -> tuple[int, str]:
        previous = "0" * 64
        seen = {previous}
        count = 0
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return 0, previous
        for index, line in enumerate(lines, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"audit log integrity check failed: invalid JSON at line {index}"
                ) from exc
            if not isinstance(record, dict):
                raise RuntimeError(
                    f"audit log integrity check failed: invalid record at line {index}"
                )
            entry_hash = record.pop("entry_hash", None)
            claimed = record.get("previous_hash")
            if claimed != previous:
                if claimed in seen:
                    raise RuntimeError(
                        "audit log integrity check failed: concurrent-writer fork at "
                        f"line {index}"
                    )
                raise RuntimeError(
                    f"audit log integrity check failed: broken chain at line {index}"
                )
            if entry_hash != self._hash(record):
                raise RuntimeError(
                    f"audit log integrity check failed: hash mismatch at line {index}"
                )
            previous = entry_hash
            seen.add(entry_hash)
            count += 1
        return count, previous

    def verify_sync(self) -> int:
        with self._lock:
            with _os_lock(self._lock_path):
                count, previous = self._verify_sync()
                self._last_hash = previous
                return count

    def repair_sync(self) -> tuple[int, Path | None, str | None]:
        """Verify the chain or atomically archive it while appenders are excluded."""

        with self._lock:
            with _os_lock(self._lock_path):
                try:
                    count, previous = self._verify_sync()
                except RuntimeError as exc:
                    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                    archive = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
                    suffix = 1
                    while archive.exists():
                        archive = self.path.with_name(
                            f"{self.path.name}.corrupt-{stamp}-{suffix}"
                        )
                        suffix += 1
                    self.path.rename(archive)
                    self._last_hash = None
                    return 0, archive, str(exc)
                self._last_hash = previous
                return count, None, None

    async def verify(self) -> int:
        """Verify the complete hash chain and return its event count."""

        return await asyncio.to_thread(self.verify_sync)

    def _record_sync(self, event: AuditEvent) -> None:
        with self._lock:
            with _os_lock(self._lock_path):
                _, previous = self._verify_sync()
                record = {**event.to_dict(), "previous_hash": previous}
                record["entry_hash"] = self._hash(record)
                line = json.dumps(record, sort_keys=True, separators=(",", ":"))
                with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(line + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                if os.name != "nt":
                    os.chmod(self.path, 0o600)
                self._last_hash = record["entry_hash"]

    async def record(self, event: AuditEvent) -> None:
        await asyncio.to_thread(self._record_sync, event)
