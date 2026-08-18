from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path


class DraftProvenanceError(RuntimeError):
    """Raised when draft provenance is absent, invalid, or mismatched."""


@dataclass(frozen=True, slots=True)
class DraftProvenance:
    draft_id: str
    account_id: str
    mailbox: str
    uid_validity: str | None
    uid: str | None
    message_id: str
    attachment_hashes: tuple[str, ...]
    created_at: str
    updated_at: str
    superseded_uid: str | None = None

    def __post_init__(self) -> None:
        if len(self.draft_id) != 32 or any(
            char not in "0123456789abcdef" for char in self.draft_id
        ):
            raise DraftProvenanceError("invalid draft_id")
        if not self.account_id or not self.mailbox or not self.message_id:
            raise DraftProvenanceError("incomplete draft provenance")
        if (self.uid_validity is None) != (self.uid is None):
            raise DraftProvenanceError("UID and UIDVALIDITY must be present together")
        for value, name in ((self.uid_validity, "UIDVALIDITY"), (self.uid, "UID")):
            if value is not None and (not value.isascii() or not value.isdigit()):
                raise DraftProvenanceError(f"invalid {name}")
        if self.superseded_uid is not None and (
            not self.superseded_uid.isascii() or not self.superseded_uid.isdigit()
        ):
            raise DraftProvenanceError("invalid superseded UID")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.attachment_hashes
        ):
            raise DraftProvenanceError("invalid attachment hash")

    @property
    def update_supported(self) -> bool:
        return self.uid_validity is not None and self.uid is not None

    def to_dict(self) -> dict:
        value = asdict(self)
        value["attachment_hashes"] = list(self.attachment_hashes)
        return value

    @classmethod
    def from_dict(cls, value: object) -> "DraftProvenance":
        if not isinstance(value, dict):
            raise DraftProvenanceError("invalid draft provenance")
        expected = {
            "draft_id",
            "account_id",
            "mailbox",
            "uid_validity",
            "uid",
            "message_id",
            "attachment_hashes",
            "created_at",
            "updated_at",
            "superseded_uid",
        }
        if isinstance(value, dict) and "superseded_uid" not in value:
            value = {**value, "superseded_uid": None}
        if set(value) != expected or not isinstance(value["attachment_hashes"], list):
            raise DraftProvenanceError("invalid draft provenance shape")
        try:
            return cls(**{**value, "attachment_hashes": tuple(value["attachment_hashes"])})
        except TypeError as exc:
            raise DraftProvenanceError("invalid draft provenance values") from exc


class FileDraftStore:
    """Atomic, user-private provenance store for MCP-created drafts."""

    def __init__(self, directory: Path) -> None:
        if not directory.is_absolute():
            raise ValueError("draft provenance directory must be absolute")
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
        self.directory = directory
        self._lock = threading.Lock()

    def _path(self, draft_id: str) -> Path:
        if len(draft_id) != 32 or any(
            char not in "0123456789abcdef" for char in draft_id
        ):
            raise DraftProvenanceError("invalid draft_id")
        return self.directory / f"{draft_id}.json"

    def _write(self, record: DraftProvenance) -> None:
        path = self._path(record.draft_id)
        temporary = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            if os.name != "nt":
                directory = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def create(
        self,
        *,
        account_id: str,
        mailbox: str,
        uid_validity: str | None,
        uid: str | None,
        message_id: str,
        attachment_hashes: tuple[str, ...],
    ) -> DraftProvenance:
        now = datetime.now(UTC).isoformat()
        record = DraftProvenance(
            draft_id=secrets.token_hex(16),
            account_id=account_id,
            mailbox=mailbox,
            uid_validity=uid_validity,
            uid=uid,
            message_id=message_id,
            attachment_hashes=attachment_hashes,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._write(record)
        return record

    def get(self, draft_id: str, account_id: str) -> DraftProvenance:
        try:
            value = json.loads(self._path(draft_id).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise DraftProvenanceError("draft provenance is unavailable") from exc
        record = DraftProvenance.from_dict(value)
        if not secrets.compare_digest(record.account_id, account_id):
            raise DraftProvenanceError("draft belongs to another account")
        return record

    def update(
        self,
        current: DraftProvenance,
        *,
        mailbox: str,
        uid_validity: str | None,
        uid: str | None,
        message_id: str,
        attachment_hashes: tuple[str, ...],
        superseded_uid: str | None = None,
    ) -> DraftProvenance:
        with self._lock:
            stored = self.get(current.draft_id, current.account_id)
            if stored != current:
                raise DraftProvenanceError("draft provenance changed concurrently")
            updated = replace(
                current,
                mailbox=mailbox,
                uid_validity=uid_validity,
                uid=uid,
                message_id=message_id,
                attachment_hashes=attachment_hashes,
                superseded_uid=superseded_uid,
                updated_at=datetime.now(UTC).isoformat(),
            )
            self._write(updated)
            return updated

    def list(self) -> tuple[DraftProvenance, ...]:
        records: list[DraftProvenance] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                records.append(DraftProvenance.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, DraftProvenanceError) as exc:
                raise DraftProvenanceError(f"invalid draft provenance file: {path.name}") from exc
        return tuple(records)

    def forget(self, draft_id: str) -> bool:
        with self._lock:
            path = self._path(draft_id)
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            return True
