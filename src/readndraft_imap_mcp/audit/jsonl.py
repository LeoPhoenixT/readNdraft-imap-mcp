from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from pathlib import Path

from .model import AuditEvent


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

    @staticmethod
    def _hash(record: dict[str, object]) -> str:
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return hashlib.sha256(payload).hexdigest()

    def _verify_sync(self) -> tuple[int, str]:
        previous = "0" * 64
        count = 0
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return 0, previous
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError("audit log integrity check failed") from exc
            if not isinstance(record, dict):
                raise RuntimeError("audit log integrity check failed")
            entry_hash = record.pop("entry_hash", None)
            if record.get("previous_hash") != previous or entry_hash != self._hash(record):
                raise RuntimeError("audit log integrity check failed")
            previous = entry_hash
            count += 1
        return count, previous

    def verify_sync(self) -> int:
        with self._lock:
            count, previous = self._verify_sync()
            self._last_hash = previous
            return count

    async def verify(self) -> int:
        """Verify the complete hash chain and return its event count."""

        return await asyncio.to_thread(self.verify_sync)

    def _record_sync(self, event: AuditEvent) -> None:
        with self._lock:
            if self._last_hash is None:
                _, self._last_hash = self._verify_sync()
            record = {**event.to_dict(), "previous_hash": self._last_hash}
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
