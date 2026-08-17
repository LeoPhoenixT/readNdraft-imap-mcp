from __future__ import annotations

import imaplib
import base64
import re
import ssl
from dataclasses import replace
from datetime import UTC, date, datetime
from email import policy
from email.parser import BytesParser
from typing import TYPE_CHECKING, Any, Iterable

from readndraft_imap_mcp.mime.parser import (
    MAX_MESSAGE_BYTES,
    attachment_metadata,
    get_attachment,
    parse_message,
    plain_text,
    safe_headers,
    sanitized_html,
)
from readndraft_imap_mcp.drafts import DraftProvenance

if TYPE_CHECKING:
    from readndraft_imap_mcp.broker.accounts import AccountConfig

from .models import (
    AttachmentContent,
    DraftCreationResult,
    DraftUpdateResult,
    FlagChange,
    HtmlContent,
    Mailbox,
    MessageContent,
    MessageIdentity,
    MoveResult,
    SearchFilters,
    SearchWindow,
    SearchResult,
)

_LIST_RE = re.compile(
    rb'^\((?P<flags>[^)]*)\)\s+(?:"(?P<delimiter>[^"]*)"|NIL)\s+'
    rb'(?P<name>"(?:[^"\\]|\\.)*"|[^\s]+)$'
)
_UID_RE = re.compile(rb"\bUID (?P<uid>[0-9]+)\b")
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE (?P<size>[0-9]+)\b")
_FLAGS_RE = re.compile(rb"\bFLAGS \((?P<flags>[^)]*)\)")
_INTERNALDATE_RE = re.compile(rb'\bINTERNALDATE "(?P<value>[^"]+)"')
_APPENDUID_RE = re.compile(rb"\[APPENDUID (?P<validity>[0-9]+) (?P<uid>[0-9]+)\]")
_APPENDUID_VALUE_RE = re.compile(rb"^(?P<validity>[0-9]+) (?P<uid>[0-9]+)$")
_COPYUID_RE = re.compile(
    rb"\[COPYUID (?P<validity>[0-9]+) (?P<source>[0-9]+) (?P<destination>[0-9]+)\]"
)
_COPYUID_VALUE_RE = re.compile(
    rb"^(?P<validity>[0-9]+) (?P<source>[0-9]+) (?P<destination>[0-9]+)$"
)
_MOVE_BLOCKED_FLAGS = frozenset(
    flag.casefold() for flag in (r"\Trash", r"\Junk", r"\Drafts", r"\Sent")
)


class ImapClientError(RuntimeError):
    """Raised when a production read-only IMAP operation fails closed."""


class ImapMovePartialError(ImapClientError):
    """Raised when a fallback copy succeeded but move completion is uncertain."""


def _expect_ok(result: tuple[str, list[Any]], operation: str) -> list[Any]:
    status, data = result
    if status != "OK":
        raise ImapClientError(f"{operation} failed with IMAP status {status}")
    return data


def _raw_mailbox_name(value: bytes) -> str:
    if value.startswith(b'"') and value.endswith(b'"'):
        value = value[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
    return value.decode("utf-8", errors="replace")


def _decode_modified_utf7(value: str) -> str:
    """Decode an IMAP modified-UTF-7 mailbox name for human display."""

    output: list[str] = []
    position = 0
    while position < len(value):
        marker = value.find("&", position)
        if marker < 0:
            output.append(value[position:])
            break
        output.append(value[position:marker])
        end = value.find("-", marker)
        if end < 0:
            return value
        encoded = value[marker + 1 : end]
        if not encoded:
            output.append("&")
        else:
            try:
                payload = encoded.replace(",", "/")
                payload += "=" * (-len(payload) % 4)
                output.append(base64.b64decode(payload).decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                return value
        position = end + 1
    return "".join(output)


def _parse_flags(head: bytes) -> tuple[str, ...]:
    match = _FLAGS_RE.search(head)
    if not match:
        raise ImapClientError("server omitted FLAGS")
    return tuple(
        item.decode("ascii", errors="replace")
        for item in match.group("flags").split()
    )


def _parse_number(head: bytes, pattern: re.Pattern[bytes], name: str) -> str:
    match = pattern.search(head)
    if not match:
        raise ImapClientError(f"server omitted {name}")
    return match.group(name).decode("ascii")


def _parse_internal_date(head: bytes) -> str:
    match = _INTERNALDATE_RE.search(head)
    if not match:
        raise ImapClientError("server omitted INTERNALDATE")
    try:
        parsed = datetime.strptime(
            match.group("value").decode("ascii"),
            "%d-%b-%Y %H:%M:%S %z",
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ImapClientError("server returned invalid INTERNALDATE") from exc
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _metadata_head(data: Iterable[Any]) -> bytes:
    """Combine FETCH metadata fragments without including message literals."""
    parts: list[bytes] = []
    for item in data:
        candidate = item[0] if isinstance(item, tuple) else item
        if isinstance(candidate, bytes):
            parts.append(candidate)
    if not parts:
        raise ImapClientError("server returned no FETCH metadata")
    return b" ".join(parts)


def _first_payload(data: Iterable[Any]) -> bytes:
    for item in data:
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
            return item[1]
    raise ImapClientError("server returned no message payload")


def _quote_search(value: str) -> str:
    if not value or len(value) > 200 or "\r" in value or "\n" in value:
        raise ValueError("search text must contain 1 to 200 characters without line breaks")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Phase 2 search currently requires ASCII text") from exc
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _quote_mailbox(value: str) -> str:
    if not value or len(value) > 1024 or any(marker in value for marker in ("\r", "\n", "\x00")):
        raise ValueError("invalid mailbox name")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_date(value: date) -> str:
    return value.strftime("%d-%b-%Y")


class ImapClient:
    """One conservative verified-TLS IMAP session for a pinned account."""

    def __init__(self, account: AccountConfig, secret: str, timeout: float = 20) -> None:
        if not secret:
            raise ValueError("secret must be non-empty")
        self.account = account
        self._secret = secret
        self.timeout = timeout
        self.connection: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "ImapClient":
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        self.connection = imaplib.IMAP4_SSL(
            self.account.hostname,
            self.account.port,
            ssl_context=context,
            timeout=self.timeout,
        )
        try:
            if self.account.auth_method == "plain":
                result = self.connection.authenticate(
                    "PLAIN",
                    lambda _: f"\0{self.account.username}\0{self._secret}".encode(),
                )
            else:
                result = self.connection.login(self.account.username, self._secret)
            _expect_ok(result, "authentication")
        except (imaplib.IMAP4.error, OSError) as exc:
            self.close()
            raise ImapClientError("IMAP authentication failed") from exc
        finally:
            self._secret = ""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @property
    def imap(self) -> imaplib.IMAP4_SSL:
        if self.connection is None:
            raise ImapClientError("IMAP client is not connected")
        return self.connection

    def close(self) -> None:
        if self.connection is None:
            return
        try:
            self.connection.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
        finally:
            self.connection = None

    def list_mailboxes(self) -> tuple[Mailbox, ...]:
        result: list[Mailbox] = []
        for line in _expect_ok(self.imap.list(), "LIST"):
            if not isinstance(line, bytes):
                continue
            match = _LIST_RE.match(line)
            if not match:
                raise ImapClientError("unsupported LIST response")
            delimiter = match.group("delimiter")
            name = _raw_mailbox_name(match.group("name"))
            result.append(
                Mailbox(
                    name=name,
                    delimiter=delimiter.decode("utf-8", errors="replace") if delimiter else None,
                    flags=tuple(
                        item.decode("ascii", errors="replace")
                        for item in match.group("flags").split()
                    ),
                    display_name=_decode_modified_utf7(name),
                )
            )
        return tuple(result)

    def discover_drafts_mailbox(self) -> Mailbox:
        candidates = [
            mailbox
            for mailbox in self.list_mailboxes()
            if any(flag.casefold() == r"\drafts".casefold() for flag in mailbox.flags)
        ]
        if len(candidates) != 1:
            raise ImapClientError("server must expose exactly one SPECIAL-USE drafts mailbox")
        return candidates[0]

    def append_draft(
        self,
        raw_message: bytes,
        message_id: str,
        attachment_hashes: tuple[str, ...],
    ) -> DraftCreationResult:
        if not raw_message or len(raw_message) > MAX_MESSAGE_BYTES:
            raise ValueError("generated draft must contain at most 50 MB")
        mailbox = self.discover_drafts_mailbox()
        data = _expect_ok(
            self.imap.append(
                _quote_mailbox(mailbox.name), r"(\Draft)", None, raw_message
            ),
            "APPEND draft",
        )
        match = self._append_uid(data)
        uid_validity = match.group("validity").decode("ascii") if match else None
        uid = match.group("uid").decode("ascii") if match else None
        return DraftCreationResult(
            account_id=self.account.account_id,
            mailbox=mailbox.name,
            uid_validity=uid_validity,
            uid=uid,
            message_id=message_id,
            attachment_hashes=attachment_hashes,
        )

    def _append_uid(self, data: Iterable[Any]) -> re.Match[bytes] | None:
        candidates = [item for item in data if isinstance(item, bytes)]
        try:
            _, response = self.imap.response("APPENDUID")
        except (AttributeError, imaplib.IMAP4.error):
            response = None
        if response:
            candidates.extend(item for item in response if isinstance(item, bytes))
        combined = b" ".join(candidates)
        match = _APPENDUID_RE.search(combined)
        if match:
            return match
        return _APPENDUID_VALUE_RE.fullmatch(combined.strip())

    def _capabilities(self) -> set[str]:
        data = _expect_ok(self.imap.capability(), "CAPABILITY")
        return {
            token.decode("ascii", errors="strict").upper()
            for item in data
            if isinstance(item, bytes)
            for token in item.split()
        }

    def _copy_uid(
        self, data: Iterable[Any], source_uid: str
    ) -> tuple[str, str] | None:
        candidates = [item for item in data if isinstance(item, bytes)]
        try:
            _, response = self.imap.response("COPYUID")
        except (AttributeError, imaplib.IMAP4.error):
            response = None
        if response:
            candidates.extend(item for item in response if isinstance(item, bytes))
        combined = b" ".join(candidates)
        match = _COPYUID_RE.search(combined)
        if match is None:
            match = _COPYUID_VALUE_RE.fullmatch(combined.strip())
        if match is None or match.group("source").decode("ascii") != source_uid:
            return None
        return (
            match.group("validity").decode("ascii"),
            match.group("destination").decode("ascii"),
        )

    def _move_mailbox(self, name: str, *, role: str) -> Mailbox:
        matches = [mailbox for mailbox in self.list_mailboxes() if mailbox.name == name]
        if len(matches) != 1:
            raise ValueError(f"{role} mailbox must be one exact existing mailbox")
        mailbox = matches[0]
        flags = {flag.casefold() for flag in mailbox.flags}
        if r"\noselect".casefold() in flags:
            raise ValueError(f"{role} mailbox is not selectable")
        if flags & _MOVE_BLOCKED_FLAGS:
            raise PermissionError(f"movement involving the {role} mailbox is prohibited")
        return mailbox

    def _verify_draft(self, record: DraftProvenance) -> None:
        if not record.update_supported:
            raise ImapClientError("draft has no stable UID provenance")
        current_uid_validity = self._select(record.mailbox, readonly=False)
        if current_uid_validity != record.uid_validity:
            raise ImapClientError("UIDVALIDITY changed; draft cannot be updated")
        data = _expect_ok(
            self.imap.uid(
                "FETCH",
                record.uid,
                "(UID FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
            ),
            "UID FETCH draft provenance",
        )
        flags = _parse_flags(_metadata_head(data))
        if r"\Draft" not in flags:
            raise ImapClientError("tracked message is no longer a draft")
        headers = BytesParser(policy=policy.default).parsebytes(
            _first_payload(data), headersonly=True
        )
        if headers.get("Message-ID") != record.message_id:
            raise ImapClientError("draft Message-ID no longer matches provenance")

    def _search_draft_uids(self, record: DraftProvenance) -> tuple[str, ...]:
        current_uid_validity = self._select(record.mailbox, readonly=False)
        if current_uid_validity != record.uid_validity:
            raise ImapClientError("UIDVALIDITY changed; draft cannot be repaired")
        data = _expect_ok(
            self.imap.uid("SEARCH", None, "HEADER", "Message-ID", record.message_id),
            "UID SEARCH draft Message-ID",
        )
        values = b" ".join(item for item in data if isinstance(item, bytes)).split()
        return tuple(value.decode("ascii", errors="strict") for value in values)

    def resolve_draft_uid(self, record: DraftProvenance) -> tuple[str, ...]:
        """Return the tracked UID, or exact Message-ID matches when it is stale."""
        try:
            self._verify_draft(record)
        except ImapClientError:
            return self._search_draft_uids(record)
        assert record.uid is not None
        return (record.uid,)

    def append_draft_update(
        self,
        record: DraftProvenance,
        raw_message: bytes,
        message_id: str,
        attachment_hashes: tuple[str, ...],
    ) -> DraftUpdateResult:
        if record.account_id != self.account.account_id:
            raise PermissionError("draft provenance belongs to another account")
        if not raw_message or len(raw_message) > MAX_MESSAGE_BYTES:
            raise ValueError("generated draft must contain at most 50 MB")
        if "UIDPLUS" not in self._capabilities():
            raise ImapClientError("crash-safe draft update requires UIDPLUS")
        destination = self.discover_drafts_mailbox()
        if destination.name != record.mailbox:
            raise ImapClientError("tracked draft mailbox is no longer the SPECIAL-USE mailbox")
        self._verify_draft(record)
        appended = self.append_draft(raw_message, message_id, attachment_hashes)
        if appended.uid_validity is None or appended.uid is None:
            raise ImapClientError("UIDPLUS server omitted APPENDUID; original draft retained")
        if appended.uid_validity != record.uid_validity:
            raise ImapClientError("replacement draft UIDVALIDITY differs; original draft retained")
        return DraftUpdateResult(
            account_id=self.account.account_id,
            draft_id=record.draft_id,
            mailbox=appended.mailbox,
            uid_validity=appended.uid_validity,
            uid=appended.uid,
            message_id=message_id,
            attachment_hashes=attachment_hashes,
            method="uidplus",
        )

    def expunge_superseded_draft(self, record: DraftProvenance, uid: str) -> None:
        """Expunge only a UID recorded as part of this tracked draft update."""
        candidate = replace(record, uid=uid)
        try:
            self._verify_draft(candidate)
        except ImapClientError:
            matches = self._search_draft_uids(candidate)
            if uid not in matches:
                return
            raise
        try:
            _expect_ok(
                self.imap.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)"),
                "UID STORE old draft deleted",
            )
            _expect_ok(self.imap.uid("EXPUNGE", uid), "UID EXPUNGE old draft")
        except Exception:
            try:
                self.imap.uid("STORE", uid, "-FLAGS.SILENT", r"(\Deleted)")
            except Exception:
                pass
            raise

    def replace_draft(
        self,
        record: DraftProvenance,
        raw_message: bytes,
        message_id: str,
        attachment_hashes: tuple[str, ...],
    ) -> DraftUpdateResult:
        if record.account_id != self.account.account_id:
            raise PermissionError("draft provenance belongs to another account")
        if not raw_message or len(raw_message) > MAX_MESSAGE_BYTES:
            raise ValueError("generated draft must contain at most 50 MB")
        capabilities = self._capabilities()
        destination = self.discover_drafts_mailbox()
        if destination.name != record.mailbox:
            raise ImapClientError("tracked draft mailbox is no longer the SPECIAL-USE mailbox")
        self._verify_draft(record)

        if "REPLACE" in capabilities:
            self.imap.literal = raw_message
            command = getattr(self.imap, "_simple_command", None)
            if command is None:
                raise ImapClientError("IMAP implementation cannot issue UID REPLACE")
            data = _expect_ok(
                command(
                    "UID",
                    "REPLACE",
                    record.uid,
                    _quote_mailbox(destination.name),
                    r"(\Draft)",
                ),
                "UID REPLACE draft",
            )
            match = self._append_uid(data)
            return DraftUpdateResult(
                account_id=self.account.account_id,
                draft_id=record.draft_id,
                mailbox=destination.name,
                uid_validity=(match.group("validity").decode("ascii") if match else None),
                uid=(match.group("uid").decode("ascii") if match else None),
                message_id=message_id,
                attachment_hashes=attachment_hashes,
                method="replace",
            )

        if "UIDPLUS" not in capabilities:
            raise ImapClientError("draft update requires REPLACE or UIDPLUS")
        appended = self.append_draft(raw_message, message_id, attachment_hashes)
        if appended.uid_validity is None or appended.uid is None:
            raise ImapClientError("UIDPLUS server omitted APPENDUID; original draft retained")
        if appended.uid_validity != record.uid_validity:
            raise ImapClientError("replacement draft UIDVALIDITY differs; original draft retained")
        try:
            _expect_ok(
                self.imap.uid("STORE", record.uid, "+FLAGS.SILENT", r"(\Deleted)"),
                "UID STORE old draft deleted",
            )
            _expect_ok(
                self.imap.uid("EXPUNGE", record.uid),
                "UID EXPUNGE old draft",
            )
        except Exception:
            try:
                self.imap.uid("STORE", record.uid, "-FLAGS.SILENT", r"(\Deleted)")
            except Exception:
                pass
            raise
        return DraftUpdateResult(
            account_id=self.account.account_id,
            draft_id=record.draft_id,
            mailbox=appended.mailbox,
            uid_validity=appended.uid_validity,
            uid=appended.uid,
            message_id=message_id,
            attachment_hashes=attachment_hashes,
            method="uidplus",
        )

    def _select(self, mailbox: str, *, readonly: bool = True) -> str:
        if not mailbox or len(mailbox) > 1024 or any(
            marker in mailbox for marker in ("\r", "\n", "\x00")
        ):
            raise ValueError("invalid mailbox name")
        operation = "EXAMINE" if readonly else "SELECT"
        # imaplib passes SELECT/EXAMINE mailbox arguments through verbatim.
        # Quote the raw LIST-returned identifier so spaces and atom-specials
        # remain one argument without altering modified-UTF-7 bytes.
        _expect_ok(
            self.imap.select(_quote_mailbox(mailbox), readonly=readonly),
            operation,
        )
        _, values = self.imap.response("UIDVALIDITY")
        if not values or not isinstance(values[0], bytes):
            raise ImapClientError("server omitted UIDVALIDITY")
        return values[0].decode("ascii", errors="strict")

    def search(
        self,
        mailbox: str,
        filters: SearchFilters,
        limit: int = 50,
    ) -> tuple[SearchResult, ...]:
        return self.search_window(mailbox, filters, limit).results

    def search_window(
        self,
        mailbox: str,
        filters: SearchFilters,
        limit: int = 50,
        *,
        before_uid: str | None = None,
        expected_uid_validity: str | None = None,
    ) -> SearchWindow:
        if not 1 <= limit <= 500:
            raise ValueError("search limit must be between 1 and 500")
        uid_validity = self._select(mailbox)
        if expected_uid_validity is not None and uid_validity != expected_uid_validity:
            raise ImapClientError("UIDVALIDITY changed; restart the search")
        if before_uid is not None and (not before_uid.isascii() or not before_uid.isdigit()):
            raise ValueError("search cursor UID must be numeric")
        _, uid_next_values = self.imap.response("UIDNEXT")
        if not uid_next_values or not isinstance(uid_next_values[0], bytes):
            raise ImapClientError("server omitted UIDNEXT")
        try:
            uid_next = int(uid_next_values[0].decode("ascii", errors="strict"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ImapClientError("server returned invalid UIDNEXT") from exc
        if uid_next < 1:
            raise ImapClientError("server returned invalid UIDNEXT")
        criteria: list[str | None] = [None]
        for key, value in (
            ("FROM", filters.sender),
            ("TO", filters.recipient),
            ("SUBJECT", filters.subject),
            ("TEXT", filters.text),
        ):
            if value is not None:
                criteria.extend((key, _quote_search(value)))
        if filters.attachment_filename is not None:
            criteria.extend(
                (
                    "HEADER",
                    "Content-Disposition",
                    _quote_search(filters.attachment_filename),
                )
            )
        if filters.after is not None:
            criteria.extend(("SINCE", _imap_date(filters.after)))
        if filters.before is not None:
            criteria.extend(("BEFORE", _imap_date(filters.before)))
        if filters.read is not None:
            criteria.append("SEEN" if filters.read else "UNSEEN")
        if filters.starred is not None:
            criteria.append("FLAGGED" if filters.starred else "UNFLAGGED")
        if len(criteria) == 1:
            criteria.append("ALL")
        upper_exclusive = min(uid_next, int(before_uid)) if before_uid else uid_next
        selected: list[str] = []
        while upper_exclusive > 1 and len(selected) <= limit:
            lower = max(1, upper_exclusive - 10_000)
            uid_range = f"{lower}:{upper_exclusive - 1}"
            data = _expect_ok(
                self.imap.uid("SEARCH", *criteria, "UID", uid_range),
                "UID SEARCH",
            )
            if sum(len(item) for item in data if isinstance(item, bytes)) > 2 * 1024 * 1024:
                raise ImapClientError("UID SEARCH response exceeded the result budget")
            chunk: list[int] = []
            try:
                for item in data:
                    if not isinstance(item, bytes):
                        continue
                    for raw_uid in item.split():
                        value = int(raw_uid.decode("ascii", errors="strict"))
                        if value < lower or value >= upper_exclusive:
                            raise ImapClientError(
                                "UID SEARCH returned a UID outside the requested range"
                            )
                        chunk.append(value)
            except (UnicodeDecodeError, ValueError) as exc:
                raise ImapClientError("UID SEARCH returned an invalid UID") from exc
            for value in sorted(set(chunk), reverse=True):
                selected.append(str(value))
                if len(selected) > limit:
                    break
            upper_exclusive = lower
        has_more = len(selected) > limit
        selected = selected[:limit]
        results: list[SearchResult] = []
        for uid in selected:
            fetched = _expect_ok(
                self.imap.uid(
                    "FETCH",
                    uid,
                    "(UID RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER.FIELDS "
                    "(DATE FROM TO CC SUBJECT MESSAGE-ID IN-REPLY-TO)])",
                ),
                "UID FETCH summary",
            )
            head = _metadata_head(fetched)
            header_message = BytesParser(policy=policy.default).parsebytes(
                _first_payload(fetched), headersonly=True
            )
            results.append(
                SearchResult(
                    identity=MessageIdentity(
                        self.account.account_id,
                        mailbox,
                        uid_validity,
                        _parse_number(head, _UID_RE, "uid"),
                    ),
                    headers=safe_headers(header_message),
                    flags=self._get_flags(uid),
                    size=int(_parse_number(head, _SIZE_RE, "size")),
                    received_at=_parse_internal_date(head),
                )
            )
        return SearchWindow(
            results=tuple(results),
            uid_validity=uid_validity,
            next_uid=(results[-1].identity.uid if has_more and results else None),
            has_more=has_more,
        )

    def _fetch_message(
        self,
        identity: MessageIdentity,
        max_source_bytes: int = MAX_MESSAGE_BYTES,
    ) -> tuple[bytes, tuple[str, ...]]:
        if identity.account_id != self.account.account_id:
            raise PermissionError("message identity belongs to another account")
        current_uid_validity = self._select(identity.mailbox)
        if current_uid_validity != identity.uid_validity:
            raise ImapClientError("UIDVALIDITY changed; message must be resolved again")
        metadata = _expect_ok(
            self.imap.uid("FETCH", identity.uid, "(UID FLAGS RFC822.SIZE)"),
            "UID FETCH metadata",
        )
        head = _metadata_head(metadata)
        before_flags = _parse_flags(head)
        size = int(_parse_number(head, _SIZE_RE, "size"))
        if size > min(MAX_MESSAGE_BYTES, max_source_bytes):
            raise ValueError("message exceeds the 50 MB retrieval limit")
        payload_data = _expect_ok(
            self.imap.uid("FETCH", identity.uid, "(BODY.PEEK[] FLAGS)"),
            "UID FETCH BODY.PEEK",
        )
        raw = _first_payload(payload_data)
        if len(raw) > max_source_bytes:
            raise ValueError("message exceeds the remaining retrieval limit")
        after_flags = _parse_flags(_metadata_head(payload_data))
        if set(before_flags) != set(after_flags):
            raise ImapClientError("BODY.PEEK changed message flags")
        return raw, before_flags

    def get_message(
        self,
        identity: MessageIdentity,
        max_source_bytes: int = MAX_MESSAGE_BYTES,
    ) -> MessageContent:
        raw, flags = self._fetch_message(identity, max_source_bytes)
        message = parse_message(raw)
        return MessageContent(
            identity=identity,
            headers=safe_headers(message),
            text=plain_text(message),
            flags=flags,
            attachments=attachment_metadata(message),
            source_size=len(raw),
        )

    def get_html(self, identity: MessageIdentity) -> HtmlContent:
        raw, flags = self._fetch_message(identity)
        return HtmlContent(
            identity=identity,
            html=sanitized_html(parse_message(raw)),
            flags=flags,
        )

    def get_attachment(
        self, identity: MessageIdentity, attachment_id: str
    ) -> AttachmentContent:
        if not re.fullmatch(r"part-[1-9][0-9]*", attachment_id):
            raise ValueError("invalid attachment_id")
        raw, _ = self._fetch_message(identity)
        return get_attachment(parse_message(raw), attachment_id)

    def _get_flags(self, uid: str) -> tuple[str, ...]:
        data = _expect_ok(
            self.imap.uid("FETCH", uid, "(UID FLAGS)"),
            "UID FETCH FLAGS",
        )
        head = _metadata_head(data)
        if _parse_number(head, _UID_RE, "uid") != uid:
            raise ImapClientError("server returned FLAGS for an unexpected UID")
        return _parse_flags(head)

    def _change_flag(
        self,
        identity: MessageIdentity,
        *,
        flag: str,
        state: str,
        enabled: bool,
    ) -> FlagChange:
        if identity.account_id != self.account.account_id:
            raise PermissionError("message identity belongs to another account")
        current_uid_validity = self._select(identity.mailbox, readonly=False)
        if current_uid_validity != identity.uid_validity:
            raise ImapClientError("UIDVALIDITY changed; message must be resolved again")
        before = self._get_flags(identity.uid)
        before_set = set(before)
        was_enabled = flag in before_set
        if was_enabled != enabled:
            action = "+FLAGS.SILENT" if enabled else "-FLAGS.SILENT"
            _expect_ok(
                self.imap.uid("STORE", identity.uid, action, f"({flag})"),
                f"UID STORE {state}",
            )
        after = self._get_flags(identity.uid)
        expected = set(before_set)
        if enabled:
            expected.add(flag)
        else:
            expected.discard(flag)
        if set(after) != expected:
            raise ImapClientError("server changed flags outside the requested semantic state")
        return FlagChange(
            identity=identity,
            state=state,
            enabled=enabled,
            changed=was_enabled != enabled,
            old_flags=before,
            new_flags=after,
        )

    def set_star(self, identity: MessageIdentity, starred: bool) -> FlagChange:
        return self._change_flag(
            identity,
            flag=r"\Flagged",
            state="starred",
            enabled=starred,
        )

    def set_read_state(self, identity: MessageIdentity, read: bool) -> FlagChange:
        return self._change_flag(
            identity,
            flag=r"\Seen",
            state="read",
            enabled=read,
        )

    def move_email(
        self, identity: MessageIdentity, destination_mailbox: str
    ) -> MoveResult:
        if identity.account_id != self.account.account_id:
            raise PermissionError("message identity belongs to another account")
        capabilities = self._capabilities()
        if "UIDPLUS" not in capabilities:
            raise ImapClientError("message movement requires UIDPLUS")
        source = self._move_mailbox(identity.mailbox, role="source")
        destination = self._move_mailbox(destination_mailbox, role="destination")
        if source.name == destination.name:
            raise ValueError("source and destination mailboxes must differ")

        current_uid_validity = self._select(source.name, readonly=False)
        if current_uid_validity != identity.uid_validity:
            raise ImapClientError("UIDVALIDITY changed; message must be resolved again")
        self._get_flags(identity.uid)

        if "MOVE" in capabilities:
            command = getattr(self.imap, "_simple_command", None)
            if command is None:
                raise ImapClientError("IMAP implementation cannot issue UID MOVE")
            data = _expect_ok(
                command("UID", "MOVE", identity.uid, _quote_mailbox(destination.name)),
                "UID MOVE message",
            )
            mapping = self._copy_uid(data, identity.uid)
            method = "uid_move"
        else:
            data = _expect_ok(
                self.imap.uid(
                    "COPY", identity.uid, _quote_mailbox(destination.name)
                ),
                "UID COPY move destination",
            )
            mapping = self._copy_uid(data, identity.uid)
            if mapping is None:
                raise ImapMovePartialError(
                    "UIDPLUS server omitted COPYUID; source retained and destination requires review"
                )
            try:
                _expect_ok(
                    self.imap.uid(
                        "STORE", identity.uid, "+FLAGS.SILENT", r"(\Deleted)"
                    ),
                    "UID STORE move source deleted",
                )
                _expect_ok(
                    self.imap.uid("EXPUNGE", identity.uid),
                    "UID EXPUNGE move source",
                )
            except Exception as exc:
                try:
                    _expect_ok(
                        self.imap.uid(
                            "STORE", identity.uid, "-FLAGS.SILENT", r"(\Deleted)"
                        ),
                        "UID STORE move rollback",
                    )
                    if r"\Deleted" in self._get_flags(identity.uid):
                        raise ImapClientError("move rollback did not clear deleted state")
                except Exception:
                    pass
                raise ImapMovePartialError(
                    "destination copied; source state requires review"
                ) from exc
            method = "uidplus_copy_delete"
        destination_identity = (
            MessageIdentity(
                identity.account_id,
                destination.name,
                mapping[0],
                mapping[1],
            )
            if mapping is not None
            else None
        )
        return MoveResult(
            identity, destination.name, destination_identity, method=method
        )
