from __future__ import annotations

import imaplib
import re
import ssl
import unicodedata
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from typing import TYPE_CHECKING, Any, Iterable

from readndraft_imap_mcp.drafts import DraftProvenance
from readndraft_imap_mcp.imap.bodystructure import BodyStructureError, MimePart, parse_bodystructure
from readndraft_imap_mcp.imap.search import (
    MAX_SEARCH_REQUESTS,
    MAX_SEARCH_RESPONSE_BYTES,
    SEARCH_FETCH_CHUNK_SIZE,
    SEARCH_UID_RANGE_SIZE,
    SearchScanBudget,
    search_criteria,
)
from readndraft_imap_mcp.mime.parser import (
    MAX_MESSAGE_BYTES,
    MAX_TEXT_BYTES,
    get_attachment,
    parse_message,
    plain_text,
    safe_headers,
    sanitize_filename,
    sanitized_html,
)

if TYPE_CHECKING:
    from readndraft_imap_mcp.broker.accounts import AccountConfig

from .errors import ImapClientError, ImapMovePartialError
from .models import (
    AttachmentContent,
    AttachmentMetadata,
    DraftCreationResult,
    DraftUpdateResult,
    FlagChange,
    HtmlContent,
    Mailbox,
    MessageContent,
    MessageIdentity,
    MoveResult,
    SearchFilters,
    SearchResult,
    SearchWindow,
)
from .parsing import (
    _APPENDUID_RE,
    _APPENDUID_VALUE_RE,
    _COPYUID_RE,
    _COPYUID_VALUE_RE,
    _LIST_RE,
    _SIZE_RE,
    _UID_RE,
    _attachment_filenames,
    _decode_modified_utf7,
    _expect_ok,
    _fetch_records,
    _first_payload,
    _header_message,
    _message_content,
    _metadata_head,
    _mime_parameter,
    _mime_token,
    _parse_flags,
    _parse_internal_date,
    _parse_number,
    _quote_mailbox,
    _raw_mailbox_name,
    _response_bytes,
    _structure_records,
)
from .parsing import (
    _FLAGS_RE as _FLAGS_RE,
)
from .parsing import (
    _INTERNALDATE_RE as _INTERNALDATE_RE,
)
from .parsing import (
    _validated_fetch_records as _validated_fetch_records,
)

_MOVE_BLOCKED_FLAGS = frozenset(flag.casefold() for flag in (r"\Trash", r"\Junk", r"\Drafts", r"\Sent"))


class ImapClient:
    """One conservative verified-TLS IMAP session for a pinned account."""

    def __init__(self, account: AccountConfig, secret: str, timeout: float = 20) -> None:
        if not secret:
            raise ValueError("secret must be non-empty")
        self.account = account
        self._secret = secret
        self.timeout = timeout
        self.connection: imaplib.IMAP4_SSL | None = None
        self._request_guard = None
        self._remaining_timeout = None

    def bind_request_guard(self, guard, remaining_timeout) -> None:
        """Internal broker hook checked before every IMAP command/property use."""
        self._request_guard = guard
        self._remaining_timeout = remaining_timeout

    def __enter__(self) -> "ImapClient":
        if self._request_guard is not None:
            self._request_guard()
        timeout = self.timeout
        if self._remaining_timeout is not None:
            timeout = min(timeout, self._remaining_timeout())
            if timeout <= 0:
                raise TimeoutError("broker request deadline expired")
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        self.connection = imaplib.IMAP4_SSL(
            self.account.hostname,
            self.account.port,
            ssl_context=context,
            timeout=timeout,
        )
        try:
            if self._request_guard is not None:
                self._request_guard()
            if self._remaining_timeout is not None:
                remaining = self._remaining_timeout()
                if remaining <= 0:
                    raise TimeoutError("broker request deadline expired")
                self.connection.sock.settimeout(min(self.timeout, remaining))
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
        if self._request_guard is not None:
            self._request_guard()
        if self.connection is not None and self._remaining_timeout is not None:
            remaining = self._remaining_timeout()
            if remaining <= 0:
                raise TimeoutError("broker request deadline expired")
            self.connection.sock.settimeout(min(self.timeout, remaining))
        if self.connection is None:
            raise ImapClientError("IMAP client is not connected")
        return self.connection

    def close(self) -> None:
        if self.connection is None:
            return
        try:
            if self._request_guard is None:
                self.connection.logout()
            else:
                try:
                    self._request_guard()
                except TimeoutError:
                    self.connection.shutdown()
                else:
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
                    flags=tuple(item.decode("ascii", errors="replace") for item in match.group("flags").split()),
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
            self.imap.append(_quote_mailbox(mailbox.name), r"(\Draft)", None, raw_message),
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

    def _copy_uid(self, data: Iterable[Any], source_uid: str) -> tuple[str, str] | None:
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
        headers = BytesParser(policy=policy.default).parsebytes(_first_payload(data), headersonly=True)
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

    def resolve_draft_operation(self, record: DraftProvenance, operation_id: str) -> tuple[str, ...]:
        """Find only a uniquely marked replacement in the same UIDVALIDITY."""
        if not operation_id or len(operation_id) != 32 or not operation_id.isascii():
            raise ValueError("invalid draft operation id")
        current = self._select(record.mailbox, readonly=False)
        if current != record.uid_validity:
            raise ImapClientError("UIDVALIDITY changed; draft update recovery is unsafe")
        data = _expect_ok(
            self.imap.uid("SEARCH", None, "HEADER", "X-ReadNdraft-Draft-Operation", operation_id),
            "UID SEARCH draft operation",
        )
        values = b" ".join(item for item in data if isinstance(item, bytes)).split()
        matches = tuple(value.decode("ascii", errors="strict") for value in values)
        if len(matches) != 1:
            return matches
        data = _expect_ok(
            self.imap.uid(
                "FETCH",
                matches[0],
                "(UID FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID X-ReadNdraft-Draft-Operation)])",
            ),
            "UID FETCH draft operation",
        )
        head = _metadata_head(data)
        if _parse_number(head, _UID_RE, "uid") != matches[0]:
            raise ImapClientError("UID FETCH returned an unexpected UID")
        if r"\Draft" not in _parse_flags(head):
            return ()
        headers = BytesParser(policy=policy.default).parsebytes(_first_payload(data), headersonly=True)
        if (
            headers.get("Message-ID") != record.message_id
            or headers.get("X-ReadNdraft-Draft-Operation") != operation_id
        ):
            return ()
        return matches

    def append_draft_update(
        self,
        record: DraftProvenance,
        raw_message: bytes,
        message_id: str,
        attachment_hashes: tuple[str, ...],
    ) -> DraftUpdateResult:
        self._validate_draft_replacement(record, raw_message)
        if "UIDPLUS" not in self._capabilities():
            raise ImapClientError("crash-safe draft update requires UIDPLUS")
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
        self._delete_and_expunge_uid(uid)

    def _validate_draft_replacement(self, record: DraftProvenance, raw_message: bytes) -> Mailbox:
        if record.account_id != self.account.account_id:
            raise PermissionError("draft provenance belongs to another account")
        if not raw_message or len(raw_message) > MAX_MESSAGE_BYTES:
            raise ValueError("generated draft must contain at most 50 MB")
        destination = self.discover_drafts_mailbox()
        if destination.name != record.mailbox:
            raise ImapClientError("tracked draft mailbox is no longer the SPECIAL-USE mailbox")
        self._verify_draft(record)
        return destination

    def _delete_and_expunge_uid(self, uid: str) -> None:
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
        capabilities = self._capabilities()
        destination = self._validate_draft_replacement(record, raw_message)

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
        assert record.uid is not None
        self._delete_and_expunge_uid(record.uid)
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
        if not mailbox or len(mailbox) > 1024 or any(marker in mailbox for marker in ("\r", "\n", "\x00")):
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
        scan_budget: SearchScanBudget | None = None,
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
        criteria = search_criteria(filters)
        upper_exclusive = min(uid_next, int(before_uid)) if before_uid else uid_next
        selected: list[str] = []
        budget = scan_budget or SearchScanBudget()
        next_frontier: int | None = None
        # A filename is not searchable safely with a top-level header SEARCH:
        # find candidates with all other predicates, then inspect only their
        # server-supplied MIME metadata.
        filename_query = (
            unicodedata.normalize("NFKC", filters.attachment_filename).casefold()
            if filters.attachment_filename is not None
            else None
        )
        exhausted = False
        while upper_exclusive > 1 and len(selected) <= limit and budget.take_search_request():
            lower = max(1, upper_exclusive - SEARCH_UID_RANGE_SIZE)
            uid_range = f"{lower}:{upper_exclusive - 1}"
            data = _expect_ok(
                self.imap.uid("SEARCH", *criteria, "UID", uid_range),
                "UID SEARCH",
            )
            if sum(len(item) for item in data if isinstance(item, bytes)) > MAX_SEARCH_RESPONSE_BYTES:
                raise ImapClientError("UID SEARCH response exceeded the result budget")
            chunk: list[int] = []
            try:
                for item in data:
                    if not isinstance(item, bytes):
                        continue
                    for raw_uid in item.split():
                        value = int(raw_uid.decode("ascii", errors="strict"))
                        if value < lower or value >= upper_exclusive:
                            raise ImapClientError("UID SEARCH returned a UID outside the requested range")
                        chunk.append(value)
            except (UnicodeDecodeError, ValueError) as exc:
                raise ImapClientError("UID SEARCH returned an invalid UID") from exc
            ordered = tuple(str(value) for value in sorted(set(chunk), reverse=True))
            if filename_query is None:
                selected.extend(ordered[: max(0, limit + 1 - len(selected))])
            else:
                remaining_candidates = budget.attachment_capacity()
                inspect = ordered[:remaining_candidates]
                if inspect:
                    for start in range(0, len(inspect), SEARCH_FETCH_CHUNK_SIZE):
                        batch = tuple(inspect[start : start + SEARCH_FETCH_CHUNK_SIZE])
                        structures = _structure_records(
                            _expect_ok(
                                self.imap.uid("FETCH", ",".join(batch), "(UID RFC822.SIZE INTERNALDATE BODYSTRUCTURE)"),
                                "UID FETCH BODYSTRUCTURE",
                            ),
                            batch,
                        )
                        budget.take_candidates(len(batch))
                        for uid in batch:
                            filenames = _attachment_filenames(structures[uid])
                            if any(
                                filename_query in unicodedata.normalize("NFKC", name).casefold()
                                for name in filenames
                            ):
                                selected.append(uid)
                                if len(selected) > limit:
                                    break
                        if len(selected) > limit:
                            break
                if len(inspect) < len(ordered):
                    exhausted = True
                    # The next invocation must resume inside this SEARCH
                    # range, rather than jumping to its lower bound.
                    next_frontier = int(inspect[-1]) if inspect else upper_exclusive
                    break
            upper_exclusive = lower
            if len(selected) > limit:
                # The extra match proves another page exists, but remains the
                # first UID to return on that page.  Re-inspecting it is safe
                # and prevents a result gap.
                next_frontier = int(selected[-1]) + 1
                break
        # `upper_exclusive` advances for every inspected UID range, so it is a
        # truthful continuation frontier even when there were zero matches.
        exhausted = exhausted or (
            upper_exclusive > 1
            and (budget.search_requests >= MAX_SEARCH_REQUESTS or budget.attachment_capacity() == 0)
        )
        has_more = len(selected) > limit or exhausted or upper_exclusive > 1
        selected = selected[:limit]
        results: list[SearchResult] = []
        for start in range(0, len(selected), SEARCH_FETCH_CHUNK_SIZE):
            chunk = tuple(selected[start : start + SEARCH_FETCH_CHUNK_SIZE])
            uid_set = ",".join(chunk)
            summaries = _fetch_records(
                _expect_ok(
                    self.imap.uid(
                        "FETCH",
                        uid_set,
                        "(UID RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER.FIELDS "
                        "(DATE FROM TO CC SUBJECT MESSAGE-ID IN-REPLY-TO)])",
                    ),
                    "UID FETCH summary",
                ),
                chunk,
                require_payload=True,
            )
            flags = _fetch_records(
                _expect_ok(
                    self.imap.uid("FETCH", uid_set, "(UID FLAGS)"),
                    "UID FETCH FLAGS",
                ),
                chunk,
                require_payload=False,
            )
            if set(summaries) != set(flags):
                raise ImapClientError("UID FETCH summary and FLAGS results differ")
            for uid in chunk:
                head, literal = summaries[uid]
                assert literal is not None
                flag_head, _ = flags[uid]
                header_message = BytesParser(policy=policy.default).parsebytes(literal, headersonly=True)
                results.append(
                    SearchResult(
                        identity=MessageIdentity(
                            self.account.account_id,
                            mailbox,
                            uid_validity,
                            uid,
                        ),
                        headers=safe_headers(header_message),
                        flags=_parse_flags(flag_head),
                        size=int(_parse_number(head, _SIZE_RE, "size")),
                        received_at=_parse_internal_date(head),
                    )
                )
        return SearchWindow(
            results=tuple(results),
            uid_validity=uid_validity,
            next_uid=(str(next_frontier if next_frontier is not None else upper_exclusive) if has_more else None),
            has_more=has_more,
            complete=not has_more,
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
            self.imap.uid("FETCH", identity.uid, "(UID BODY.PEEK[] FLAGS)"),
            "UID FETCH BODY.PEEK",
        )
        payload_records = _fetch_records(payload_data, (identity.uid,), require_payload=True)
        payload_head, raw = payload_records[identity.uid]
        assert raw is not None
        if len(raw) > max_source_bytes:
            raise ValueError("message exceeds the remaining retrieval limit")
        after_flags = _parse_flags(payload_head)
        if set(before_flags) != set(after_flags):
            raise ImapClientError("BODY.PEEK changed message flags")
        return raw, before_flags

    def _fetch_structure(self, identity: MessageIdentity) -> tuple[MimePart, bytes, tuple[str, ...], int]:
        """Read headers and BODYSTRUCTURE, without any MIME body literal."""
        if identity.account_id != self.account.account_id:
            raise PermissionError("message identity belongs to another account")
        if self._select(identity.mailbox) != identity.uid_validity:
            raise ImapClientError("UIDVALIDITY changed; message must be resolved again")
        data = _expect_ok(
            self.imap.uid(
                "FETCH",
                identity.uid,
                "(UID FLAGS RFC822.SIZE BODYSTRUCTURE "
                "BODY.PEEK[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID IN-REPLY-TO)])",
            ),
            "UID FETCH BODYSTRUCTURE",
        )
        records = _fetch_records(data, (identity.uid,), require_payload=True)
        head, headers = records[identity.uid]
        assert headers is not None
        flags = _parse_flags(head)
        size = int(_parse_number(head, _SIZE_RE, "size"))
        if size > MAX_MESSAGE_BYTES:
            raise ValueError("message exceeds the 50 MB retrieval limit")
        # A malformed BODYSTRUCTURE is not an IMAP transport failure.  Callers
        # deliberately retain the bounded full-message fallback for it.
        structure = parse_bodystructure(_response_bytes(data))
        return structure, headers, flags, size

    def _fetch_section(self, identity: MessageIdentity, section: str, maximum: int) -> bytes:
        if not re.fullmatch(r"[1-9][0-9]*(?:\.[1-9][0-9]*)*", section):
            raise ImapClientError("invalid BODYSTRUCTURE section")
        data = _expect_ok(self.imap.uid("FETCH", identity.uid, f"(UID BODY.PEEK[{section}])"), "UID FETCH body section")
        records = _fetch_records(data, (identity.uid,), require_payload=True)
        head, payload = records[identity.uid]
        if f"BODY[{section}]".encode() not in head.upper() and f"BODY.PEEK[{section}]".encode() not in head.upper():
            raise ImapClientError("server returned a different BODY section")
        assert payload is not None
        if len(payload) > maximum:
            raise ValueError("selected MIME body exceeds the retrieval limit")
        return payload

    @staticmethod
    def _body_candidates(root: MimePart) -> tuple[list[MimePart], list[MimePart], list[MimePart]]:
        plain: list[MimePart] = []
        html: list[MimePart] = []
        attachments: list[MimePart] = []

        def visit(part: MimePart, inherited_attachment: bool = False) -> None:
            blocked = inherited_attachment or part.is_attachment
            if part.content_type == "message/rfc822":
                # Existing parser deliberately does not descend into attached messages.
                return
            if part.children:
                children = part.children
                if part.content_type == "multipart/related" and children:
                    start = (part.related_start or "").strip().strip("<>")
                    children = tuple(
                        child
                        for child in children
                        if (child.content_id or "").strip().strip("<>") == start
                    ) or (children[0],)
                for child in children:
                    visit(child, blocked)
                return
            if blocked:
                attachments.append(part)
                return
            if part.content_type == "text/plain":
                plain.append(part)
            elif part.content_type == "text/html":
                html.append(part)

        visit(root)
        return plain, html, attachments

    @staticmethod
    def _part_message(part: MimePart, payload: bytes, *, filename: str | None = None):
        """Build one standalone MIME entity for a BODY.PEEK section literal.

        A HEADER.FIELDS literal can already end with its required empty line.
        It must therefore never be prefixed to a selected part: doing so turns
        this entity's Content-Type and CTE fields into body text.
        """
        content_type = _mime_token(part.content_type, "application/octet-stream")
        cte = _mime_token(part.transfer_encoding or "7bit", "7bit")
        # Charset is the only Content-Type parameter required to decode a
        # selected text part.  Do not turn arbitrary server metadata into
        # synthetic MIME headers.
        parameters = b"".join(
            parameter
            for name, value in part.content_type_params
            if name.casefold() == "charset" and (parameter := _mime_parameter(name, value)) is not None
        )
        header = b"Content-Type: " + content_type + parameters + b"\r\n"
        if filename is not None:
            quoted = _mime_parameter("filename", sanitize_filename(filename))
            assert quoted is not None
            header += b"Content-Disposition: attachment" + quoted + b"\r\n"
        header += b"Content-Transfer-Encoding: " + cte + b"\r\n\r\n"
        return parse_message(header + payload)

    def _structured_message(
        self,
        identity: MessageIdentity,
        max_source_bytes: int,
        prepared: tuple[MimePart, bytes, tuple[str, ...], int] | None = None,
    ) -> MessageContent | None:
        if prepared is None:
            try:
                root, headers, flags, _ = self._fetch_structure(identity)
            except ImapClientError:
                raise
            except Exception:
                return None
        else:
            root, headers, flags, _ = prepared
        plain, html, attachments = self._body_candidates(root)

        # message/rfc822 is a valid structure for which a faithful partial
        # reconstruction is unsafe; retain bounded legacy parsing.
        def has_rfc(part: MimePart) -> bool:
            return part.content_type == "message/rfc822" or any(has_rfc(child) for child in part.children)

        if has_rfc(root):
            return None
        chosen_text = ""
        selected_size = 0
        for part in [*plain, *html]:
            if part.section is None or part.encoded_size > MAX_TEXT_BYTES:
                if part.encoded_size > MAX_TEXT_BYTES:
                    raise ValueError("selected MIME body exceeds the 2 MB limit")
                continue
            payload = self._fetch_section(identity, part.section, MAX_TEXT_BYTES)
            message = self._part_message(part, payload)
            value = plain_text(message)
            if value.strip():
                chosen_text = value
                selected_size = len(headers) + len(payload)
                break
        metadata = tuple(
            AttachmentMetadata(
                attachment_id=part.part_id,
                filename=sanitize_filename(part.filename),
                content_type=part.content_type,
                size=None,
                encoded_size=part.encoded_size,
            )
            for part in attachments
        )
        if selected_size > max_source_bytes:
            raise ValueError("message exceeds the remaining retrieval limit")
        return MessageContent(
            identity, safe_headers(_header_message(headers)), chosen_text, flags, metadata, selected_size
        )

    def get_message(
        self,
        identity: MessageIdentity,
        max_source_bytes: int = MAX_MESSAGE_BYTES,
    ) -> MessageContent:
        structured = self._structured_message(identity, max_source_bytes)
        if structured is not None:
            return structured
        raw, flags = self._fetch_message(identity, max_source_bytes)
        return _message_content(identity, raw, flags)

    def get_message_budgeted(self, identity: MessageIdentity, reserve_source) -> MessageContent:
        """Reserve known source bytes before downloading a selected MIME body.

        BODYSTRUCTURE gives transfer-encoded section sizes.  Attached-message
        and malformed structures retain the bounded full-message path, using
        RFC822.SIZE as their conservative reservation.
        """
        try:
            root, headers, flags, message_size = self._fetch_structure(identity)
            plain, html, _ = self._body_candidates(root)
            def has_rfc(part: MimePart) -> bool:
                return part.content_type == "message/rfc822" or any(has_rfc(child) for child in part.children)

            has_rfc = has_rfc(root)
            estimated = message_size if has_rfc else min(
                message_size,
                len(headers) + sum(part.encoded_size for part in [*plain, *html]),
            )
            if not reserve_source(estimated):
                raise ValueError("message exceeds the remaining retrieval limit")
            structured = self._structured_message(
                identity,
                MAX_MESSAGE_BYTES,
                (root, headers, flags, message_size),
            )
            if structured is not None:
                return structured
            raw, flags = self._fetch_message(identity, MAX_MESSAGE_BYTES)
            return _message_content(identity, raw, flags)
        except BodyStructureError:
            # No trustworthy section size: fetch metadata only, then reserve
            # the server-advertised whole-message size before BODY.PEEK[].
            if identity.account_id != self.account.account_id:
                raise PermissionError("message identity belongs to another account")
            if self._select(identity.mailbox) != identity.uid_validity:
                raise ImapClientError("UIDVALIDITY changed; message must be resolved again")
            data = _expect_ok(self.imap.uid("FETCH", identity.uid, "(UID FLAGS RFC822.SIZE)"), "UID FETCH metadata")
            head = _metadata_head(data)
            if _parse_number(head, _UID_RE, "uid") != identity.uid:
                raise ImapClientError("UID FETCH returned a different message")
            if not reserve_source(int(_parse_number(head, _SIZE_RE, "size"))):
                raise ValueError("message exceeds the remaining retrieval limit")
            raw, flags = self._fetch_message(identity, MAX_MESSAGE_BYTES)
            return _message_content(identity, raw, flags)

    def get_threading_headers(self, identity: MessageIdentity) -> tuple[str, str | None]:
        """Read only reply-thread headers without marking the source read."""
        if identity.account_id != self.account.account_id:
            raise PermissionError("message identity belongs to another account")
        if self._select(identity.mailbox) != identity.uid_validity:
            raise ImapClientError("UIDVALIDITY changed; message must be resolved again")
        data = _expect_ok(
            self.imap.uid("FETCH", identity.uid, "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES)])"),
            "UID FETCH threading headers",
        )
        head = _metadata_head(data)
        if _parse_number(head, _UID_RE, "uid") != identity.uid:
            raise ImapClientError("UID FETCH returned a different message")
        message = BytesParser(policy=policy.default).parsebytes(_first_payload(data), headersonly=True)
        message_id = message.get("Message-ID")
        if not isinstance(message_id, str):
            raise ValueError("source message has no Message-ID")
        references = message.get("References")
        return message_id, references if isinstance(references, str) else None

    def get_html(self, identity: MessageIdentity) -> HtmlContent:
        try:
            root, headers, flags, _ = self._fetch_structure(identity)
            _, html, _ = self._body_candidates(root)
            for part in html:
                if part.section is None:
                    continue
                if part.encoded_size > MAX_TEXT_BYTES:
                    raise ValueError("selected MIME body exceeds the 2 MB limit")
                payload = self._fetch_section(identity, part.section, MAX_TEXT_BYTES)
                value = sanitized_html(self._part_message(part, payload))
                if value.strip():
                    return HtmlContent(identity, value, flags)
            return HtmlContent(identity, "", flags)
        except BodyStructureError:
            pass
        raw, flags = self._fetch_message(identity)
        return HtmlContent(
            identity=identity,
            html=sanitized_html(parse_message(raw)),
            flags=flags,
        )

    def get_attachment(self, identity: MessageIdentity, attachment_id: str) -> AttachmentContent:
        if not re.fullmatch(r"part-[1-9][0-9]*", attachment_id):
            raise ValueError("invalid attachment_id")
        try:
            root, _, _, _ = self._fetch_structure(identity)
            _, _, attachments = self._body_candidates(root)
            part = next((item for item in attachments if item.part_id == attachment_id), None)
            if part is None or part.section is None:
                raise KeyError("unknown attachment_id")
            if part.encoded_size > 25 * 1024 * 1024:
                raise ValueError("attachment exceeds the 25 MB limit")
            payload = self._fetch_section(identity, part.section, 25 * 1024 * 1024)
            # Reuse the standard decoder with a standalone, safely quoted MIME entity.
            content = get_attachment(self._part_message(part, payload, filename=part.filename), "part-1")
            return replace(
                content, metadata=replace(content.metadata, attachment_id=attachment_id, encoded_size=part.encoded_size)
            )
        except BodyStructureError:
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

    def move_email(self, identity: MessageIdentity, destination_mailbox: str) -> MoveResult:
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
                self.imap.uid("COPY", identity.uid, _quote_mailbox(destination.name)),
                "UID COPY move destination",
            )
            mapping = self._copy_uid(data, identity.uid)
            if mapping is None:
                raise ImapMovePartialError(
                    "UIDPLUS server omitted COPYUID; source retained and destination requires review"
                )
            try:
                _expect_ok(
                    self.imap.uid("STORE", identity.uid, "+FLAGS.SILENT", r"(\Deleted)"),
                    "UID STORE move source deleted",
                )
                _expect_ok(
                    self.imap.uid("EXPUNGE", identity.uid),
                    "UID EXPUNGE move source",
                )
            except Exception as exc:
                try:
                    _expect_ok(
                        self.imap.uid("STORE", identity.uid, "-FLAGS.SILENT", r"(\Deleted)"),
                        "UID STORE move rollback",
                    )
                    if r"\Deleted" in self._get_flags(identity.uid):
                        raise ImapClientError("move rollback did not clear deleted state")
                except Exception:
                    pass
                raise ImapMovePartialError("destination copied; source state requires review") from exc
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
        return MoveResult(identity, destination.name, destination_identity, method=method)
