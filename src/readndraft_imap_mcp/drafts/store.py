from __future__ import annotations

import json
import os
import secrets
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path


class DraftProvenanceError(RuntimeError):
    """Raised when draft provenance is absent, invalid, or mismatched."""


class DraftBusyError(DraftProvenanceError):
    """Raised when another process is changing the same draft."""


class DraftRecoveryRequiredError(DraftProvenanceError):
    """Raised when an interrupted update cannot be proved safe to resume."""


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
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()

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
        if (self.in_reply_to is None) != (not self.references):
            raise DraftProvenanceError("invalid draft threading metadata")

    @property
    def update_supported(self) -> bool:
        return self.uid_validity is not None and self.uid is not None

    def to_dict(self) -> dict:
        value = asdict(self)
        value["attachment_hashes"] = list(self.attachment_hashes)
        value["references"] = list(self.references)
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
            "in_reply_to",
            "references",
        }
        if isinstance(value, dict):
            value = {
                **value,
                "superseded_uid": value.get("superseded_uid"),
                "in_reply_to": value.get("in_reply_to"),
                "references": value.get("references", []),
            }
        if (
            set(value) != expected
            or not isinstance(value["attachment_hashes"], list)
            or not isinstance(value["references"], list)
        ):
            raise DraftProvenanceError("invalid draft provenance shape")
        try:
            return cls(
                **{
                    **value,
                    "attachment_hashes": tuple(value["attachment_hashes"]),
                    "references": tuple(value["references"]),
                }
            )
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

    @contextmanager
    def operation_lock(self, draft_id: str):
        """Acquire a per-draft cross-process lock without ever queueing updates."""
        path = self._path(draft_id).with_suffix(".lock")
        stream = path.open("a+b")
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                try:
                    if os.fstat(stream.fileno()).st_size == 0:
                        stream.write(b"0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError as exc:
                    raise DraftBusyError("draft update is already in progress") from exc
            else:
                import fcntl
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError as exc:
                    raise DraftBusyError("draft update is already in progress") from exc
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    def _journal_path(self, draft_id: str) -> Path:
        return self._path(draft_id).with_suffix(".operation.json")

    def _sync_directory(self) -> None:
        """Make a preceding rename or unlink durable on POSIX filesystems."""
        if os.name == "nt":
            return
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def write_operation(self, draft_id: str, value: dict) -> None:
        """Durably record an update phase before its external IMAP effect."""
        path = self._journal_path(draft_id)
        temporary = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(value, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            self._sync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def get_operation(self, draft_id: str) -> dict | None:
        path = self._journal_path(draft_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise DraftRecoveryRequiredError("draft update recovery is required") from exc
        legacy = {"v", "operation_id", "account_id", "mailbox", "uid_validity", "old_uid", "phase"}
        current = legacy | {"message_id", "attachment_hashes"}
        allowed = (
            legacy,
            legacy | {"new_uid"},
            current,
            current | {"new_uid"},
        )
        if not isinstance(value, dict) or set(value) not in allowed:
            raise DraftRecoveryRequiredError("draft update recovery is required")
        if (
            value.get("v") not in {1, 2}
            or not isinstance(value.get("operation_id"), str)
            or len(value["operation_id"]) != 32
            or any(char not in "0123456789abcdef" for char in value["operation_id"])
            or not isinstance(value.get("account_id"), str)
            or not isinstance(value.get("mailbox"), str)
            or not isinstance(value.get("uid_validity"), str) or not value["uid_validity"].isdigit()
            or not isinstance(value.get("old_uid"), str) or not value["old_uid"].isdigit()
            or value.get("phase") not in {"prepared", "appended"}
            or ("new_uid" in value and (not isinstance(value["new_uid"], str) or not value["new_uid"].isdigit()))
        ):
            raise DraftRecoveryRequiredError("draft update recovery is required")
        if value["v"] == 2 and (
            set(value) not in (current, current | {"new_uid"})
            or not isinstance(value.get("message_id"), str)
            or not value["message_id"]
            or not isinstance(value.get("attachment_hashes"), list)
            or any(
                not isinstance(item, str)
                or len(item) != 64
                or any(char not in "0123456789abcdef" for char in item)
                for item in value["attachment_hashes"]
            )
        ):
            raise DraftRecoveryRequiredError("draft update recovery is required")
        return value

    @staticmethod
    def validate_operation(record: DraftProvenance, operation: dict) -> tuple[str, ...]:
        """Validate a journal against its provenance before any server recovery.

        Version-one journals predate replacement attachment hashes.  They stay
        readable so operators can inspect them, but cannot safely be adopted.
        """
        if (
            operation.get("v") != 2
            or operation.get("account_id") != record.account_id
            or operation.get("mailbox") != record.mailbox
            or operation.get("uid_validity") != record.uid_validity
            or operation.get("message_id") != record.message_id
            or not isinstance(operation.get("attachment_hashes"), list)
        ):
            raise DraftRecoveryRequiredError("draft update recovery is required")
        if operation["old_uid"] == record.uid:
            return tuple(operation["attachment_hashes"])
        if (
            operation.get("new_uid") == record.uid
            and record.superseded_uid == operation["old_uid"]
        ):
            return tuple(operation["attachment_hashes"])
        raise DraftRecoveryRequiredError("draft update recovery is required")

    def clear_operation(self, draft_id: str) -> None:
        self._journal_path(draft_id).unlink(missing_ok=True)
        self._sync_directory()

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
            self._sync_directory()
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
        in_reply_to: str | None = None,
        references: tuple[str, ...] = (),
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
            in_reply_to=in_reply_to,
            references=references,
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
            if path.name.endswith(".operation.json"):
                continue
            try:
                records.append(DraftProvenance.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, DraftProvenanceError) as exc:
                raise DraftProvenanceError(f"invalid draft provenance file: {path.name}") from exc
        return tuple(records)

    def forget(self, draft_id: str) -> bool:
        with self._lock:
            path = self._path(draft_id)
            journal = self._journal_path(draft_id)
            removed = False
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            else:
                removed = True
            try:
                journal.unlink()
            except FileNotFoundError:
                pass
            else:
                removed = True
            if removed:
                self._sync_directory()
            return removed
