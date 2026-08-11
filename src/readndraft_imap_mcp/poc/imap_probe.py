from __future__ import annotations

import imaplib
import re
import ssl
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import default
from email.utils import format_datetime, make_msgid
from typing import Any, Iterable, Literal

_LIST_RE = re.compile(
    rb'^\((?P<flags>[^)]*)\)\s+(?:"(?P<delimiter>[^"]*)"|NIL)\s+'
    rb'(?P<name>"(?:[^"\\]|\\.)*"|[^\s]+)$'
)
_FLAGS_RE = re.compile(rb"FLAGS \((?P<flags>[^)]*)\)")


class ProbeError(RuntimeError):
    """Raised when an IMAP probe cannot safely complete."""


@dataclass(frozen=True)
class Mailbox:
    name: str
    delimiter: str | None
    flags: tuple[str, ...]

    @property
    def is_drafts(self) -> bool:
        return any(flag.casefold() == r"\drafts" for flag in self.flags)


def _decode_quoted(value: bytes) -> str:
    if value.startswith(b'"') and value.endswith(b'"'):
        value = value[1:-1]
        value = value.replace(b'\\\\"', b'"').replace(b"\\\\", b"\\")
    return value.decode("utf-8", errors="replace")


def parse_mailbox_list(lines: Iterable[bytes | None]) -> list[Mailbox]:
    mailboxes: list[Mailbox] = []
    for line in lines:
        if not line:
            continue
        match = _LIST_RE.match(line)
        if not match:
            raise ProbeError(f"Unsupported LIST response shape: {line[:120]!r}")
        flags = tuple(
            item.decode("ascii", errors="replace")
            for item in match.group("flags").split()
        )
        delimiter_raw = match.group("delimiter")
        mailboxes.append(
            Mailbox(
                name=_decode_quoted(match.group("name")),
                delimiter=(
                    delimiter_raw.decode("utf-8", errors="replace")
                    if delimiter_raw is not None
                    else None
                ),
                flags=flags,
            )
        )
    return mailboxes


def _expect_ok(result: tuple[str, list[Any]], operation: str) -> list[Any]:
    status, data = result
    if status != "OK":
        raise ProbeError(f"{operation} failed with IMAP status {status}")
    return data


def _extract_flags(fetch_data: Iterable[Any]) -> set[str]:
    for item in fetch_data:
        head = item[0] if isinstance(item, tuple) else item
        if isinstance(head, bytes):
            match = _FLAGS_RE.search(head)
            if match:
                return {
                    value.decode("ascii", errors="replace")
                    for value in match.group("flags").split()
                }
    raise ProbeError("Server did not return FLAGS for the selected UID")


def _payload_size(fetch_data: Iterable[Any]) -> int:
    return sum(
        len(item[1])
        for item in fetch_data
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes)
    )


class ImapProbe:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        secret: str,
        timeout: float = 20,
        auth_method: Literal["login", "plain"] = "login",
    ):
        if not host.strip() or not username.strip() or not secret:
            raise ValueError("host, username, and secret are required")
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if auth_method not in {"login", "plain"}:
            raise ValueError("auth_method must be 'login' or 'plain'")
        self.host = host
        self.port = port
        self.username = username
        self._secret = secret
        self.timeout = timeout
        self.auth_method = auth_method
        self.connection: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "ImapProbe":
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        self.connection = imaplib.IMAP4_SSL(
            self.host,
            self.port,
            ssl_context=context,
            timeout=self.timeout,
        )
        try:
            if self.auth_method == "plain":
                result = self.connection.authenticate(
                    "PLAIN",
                    lambda _: f"\0{self.username}\0{self._secret}".encode("utf-8"),
                )
                _expect_ok(result, "AUTHENTICATE PLAIN")
            else:
                _expect_ok(self.connection.login(self.username, self._secret), "LOGIN")
        except imaplib.IMAP4.error as exc:
            try:
                self.connection.shutdown()
            except OSError:
                pass
            self.connection = None
            raise ProbeError(f"{self.auth_method.upper()} authentication failed") from exc
        finally:
            self._secret = ""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.connection is None:
            return
        try:
            self.connection.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    @property
    def imap(self) -> imaplib.IMAP4_SSL:
        if self.connection is None:
            raise ProbeError("Probe is not connected")
        return self.connection

    def capabilities(self) -> list[str]:
        data = _expect_ok(self.imap.capability(), "CAPABILITY")
        return sorted(
            token.decode("ascii", errors="replace")
            for line in data
            if isinstance(line, bytes)
            for token in line.split()
        )

    def mailboxes(self) -> list[Mailbox]:
        return parse_mailbox_list(_expect_ok(self.imap.list(), "LIST"))

    def select(self, mailbox: str, *, readonly: bool) -> str | None:
        _expect_ok(self.imap.select(mailbox, readonly=readonly), "SELECT")
        _, values = self.imap.response("UIDVALIDITY")
        if not values or not isinstance(values[0], bytes):
            return None
        return values[0].decode("ascii", errors="replace")

    def find_unique_uid_by_subject(self, mailbox: str, subject: str) -> str:
        subject = subject.strip()
        if not subject:
            raise ValueError("subject must be non-empty")
        if len(subject) > 200:
            raise ValueError("subject must be at most 200 characters")
        if "\r" in subject or "\n" in subject:
            raise ValueError("subject must not contain line breaks")
        try:
            subject.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("Phase 0 subject lookup currently requires ASCII") from exc

        self.select(mailbox, readonly=True)
        quoted_subject = '"' + subject.replace("\\", "\\\\").replace('"', '\\"') + '"'
        data = _expect_ok(
            self.imap.uid("SEARCH", None, "HEADER", "SUBJECT", quoted_subject),
            "UID SEARCH SUBJECT",
        )
        uids = [
            uid.decode("ascii", errors="strict")
            for item in data
            if isinstance(item, bytes)
            for uid in item.split()
        ]
        if not uids:
            raise ProbeError("No message matched that subject in the selected mailbox")
        if len(uids) != 1:
            raise ProbeError(
                f"Subject lookup matched {len(uids)} messages; use a more unique subject"
            )
        return uids[0]

    def peek_message(self, mailbox: str, uid: str) -> dict[str, Any]:
        uid_validity = self.select(mailbox, readonly=True)
        before = _extract_flags(
            _expect_ok(self.imap.uid("FETCH", uid, "(FLAGS)"), "UID FETCH FLAGS")
        )
        payload = _expect_ok(
            self.imap.uid("FETCH", uid, "(BODY.PEEK[] FLAGS)"),
            "UID FETCH BODY.PEEK",
        )
        after = _extract_flags(
            _expect_ok(self.imap.uid("FETCH", uid, "(FLAGS)"), "UID FETCH FLAGS")
        )
        if before != after:
            raise ProbeError("BODY.PEEK changed message flags")
        return {
            "mailbox": mailbox,
            "uid": uid,
            "uid_validity": uid_validity,
            "bytes_retrieved": _payload_size(payload),
            "seen_before": r"\Seen" in before,
            "seen_after": r"\Seen" in after,
            "flags_preserved": True,
        }

    def probe_star_restore(self, mailbox: str, uid: str) -> dict[str, Any]:
        uid_validity = self.select(mailbox, readonly=False)
        before = _extract_flags(
            _expect_ok(self.imap.uid("FETCH", uid, "(FLAGS)"), "UID FETCH FLAGS")
        )
        originally_starred = r"\Flagged" in before
        action = "-FLAGS.SILENT" if originally_starred else "+FLAGS.SILENT"
        restore = "+FLAGS.SILENT" if originally_starred else "-FLAGS.SILENT"
        try:
            _expect_ok(
                self.imap.uid("STORE", uid, action, r"(\Flagged)"),
                "UID STORE fixed star flag",
            )
            changed = _extract_flags(
                _expect_ok(self.imap.uid("FETCH", uid, "(FLAGS)"), "UID FETCH FLAGS")
            )
            changed_as_expected = (r"\Flagged" in changed) != originally_starred
        finally:
            _expect_ok(
                self.imap.uid("STORE", uid, restore, r"(\Flagged)"),
                "UID STORE restore star flag",
            )
        final = _extract_flags(
            _expect_ok(self.imap.uid("FETCH", uid, "(FLAGS)"), "UID FETCH FLAGS")
        )
        if final != before:
            raise ProbeError("The original flag set was not restored")
        return {
            "mailbox": mailbox,
            "uid": uid,
            "uid_validity": uid_validity,
            "originally_starred": originally_starred,
            "changed_as_expected": changed_as_expected,
            "original_flags_restored": True,
        }

    def append_test_draft(self, mailbox: str) -> dict[str, Any]:
        now = datetime.now(UTC)
        message = EmailMessage(policy=default.clone(linesep="\r\n"))
        message["From"] = self.username
        message["To"] = self.username
        message["Date"] = format_datetime(now)
        message["Message-ID"] = make_msgid(domain="readndraft.local")
        message["Subject"] = f"readNdraft Phase 0 draft PoC {now.isoformat()}"
        message["X-readNdraft-PoC"] = "phase-0"
        message.set_content(
            "This is a harmless IMAP draft created by the readNdraft Phase 0 "
            "proof of concept. It was not sent."
        )
        data = _expect_ok(
            self.imap.append(mailbox, r"(\Draft)", None, message.as_bytes()),
            "APPEND draft",
        )
        _, append_uid = self.imap.response("APPENDUID")
        return {
            "mailbox": mailbox,
            "subject": message["Subject"],
            "message_id": message["Message-ID"],
            "append_response": [
                item.decode("ascii", errors="replace")
                for item in data
                if isinstance(item, bytes)
            ],
            "append_uid": [
                item.decode("ascii", errors="replace")
                for item in (append_uid or [])
                if isinstance(item, bytes)
            ],
            "manual_client_sync_required": True,
        }


def base_report(probe: ImapProbe) -> dict[str, Any]:
    capabilities = probe.capabilities()
    mailboxes = probe.mailboxes()
    folded = {capability.casefold() for capability in capabilities}
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "server": {"host": probe.host, "port": probe.port},
        "credentials_in_report": False,
        "capabilities": capabilities,
        "support": {
            "imap4rev1": "imap4rev1" in folded,
            "imap4rev2": "imap4rev2" in folded,
            "special_use": "special-use" in folded,
            "uidplus": "uidplus" in folded,
            "replace": "replace" in folded,
        },
        "mailboxes": [asdict(mailbox) for mailbox in mailboxes],
        "draft_mailboxes": [mailbox.name for mailbox in mailboxes if mailbox.is_drafts],
    }

