"""Pure IMAP response parsing and message conversion helpers."""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime
from typing import Any, Iterable

from readndraft_imap_mcp.mime.parser import attachment_metadata, parse_message, plain_text, safe_headers

from .bodystructure import BodyStructureError, MimePart, parse_bodystructure
from .errors import ImapClientError
from .models import MessageContent, MessageIdentity

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


_COPYUID_RE = re.compile(rb"\[COPYUID (?P<validity>[0-9]+) (?P<source>[0-9]+) (?P<destination>[0-9]+)\]")


_COPYUID_VALUE_RE = re.compile(rb"^(?P<validity>[0-9]+) (?P<source>[0-9]+) (?P<destination>[0-9]+)$")




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
    return tuple(item.decode("ascii", errors="replace") for item in match.group("flags").split())


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


def _response_bytes(data: Iterable[Any]) -> bytes:
    """Reassemble IMAP response fragments without altering literal octets.

    ``imaplib`` returns a literal as the second element of a tuple and may
    split the surrounding response across later tuple/continuation entries.
    Separating those values with whitespace corrupts literal framing, so this
    helper deliberately preserves their wire order.
    """
    parts: list[bytes] = []
    for item in data:
        if isinstance(item, tuple):
            if not item or not all(isinstance(value, bytes) for value in item):
                raise ImapClientError("server returned invalid FETCH metadata")
            parts.extend(item)
        elif isinstance(item, bytes):
            parts.append(item)
        elif item is not None:
            raise ImapClientError("server returned invalid FETCH metadata")
    if not parts:
        raise ImapClientError("server returned no FETCH metadata")
    return b"".join(parts)


def _first_payload(data: Iterable[Any]) -> bytes:
    for item in data:
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
            return item[1]
    raise ImapClientError("server returned no message payload")


def _fetch_records(
    data: Iterable[Any], expected_uids: tuple[str, ...], *, require_payload: bool
) -> dict[str, tuple[bytes, bytes | None]]:
    """Return one strictly validated metadata/payload pair per requested UID."""
    records: list[tuple[list[bytes], bytes | None]] = []
    if not require_payload:
        for item in data:
            if isinstance(item, bytes):
                records.append(([item], None))
            elif isinstance(item, tuple) and item and isinstance(item[0], bytes):
                records.append(([item[0]], None))
            elif item is not None:
                raise ImapClientError("server returned invalid FETCH metadata")
        if not records:
            raise ImapClientError("server returned invalid FETCH metadata")
        return _validated_fetch_records(records, expected_uids, require_payload=False)

    prefix: list[bytes] = []
    current: list[bytes] | None = None
    payload: bytes | None = None
    for item in data:
        if isinstance(item, tuple):
            if current is not None:
                records.append((current, payload))
            head = item[0] if item and isinstance(item[0], bytes) else None
            if head is None:
                raise ImapClientError("server returned invalid FETCH metadata")
            current = [*prefix, head]
            prefix = []
            payload = item[1] if len(item) > 1 and isinstance(item[1], bytes) else None
        elif isinstance(item, bytes):
            if current is None:
                prefix.append(item)
            else:
                current.append(item)
        elif item is not None:
            raise ImapClientError("server returned invalid FETCH metadata")
    if current is not None:
        records.append((current, payload))
    if prefix or not records:
        raise ImapClientError("server returned invalid FETCH metadata")
    return _validated_fetch_records(records, expected_uids, require_payload=True)


def _validated_fetch_records(
    records: Iterable[tuple[list[bytes], bytes | None]],
    expected_uids: tuple[str, ...],
    *,
    require_payload: bool,
) -> dict[str, tuple[bytes, bytes | None]]:
    values: dict[str, tuple[bytes, bytes | None]] = {}
    expected = set(expected_uids)
    for fragments, literal in records:
        head = b" ".join(fragments)
        uid = _parse_number(head, _UID_RE, "uid")
        if uid not in expected:
            raise ImapClientError("UID FETCH returned an unexpected UID")
        if uid in values:
            raise ImapClientError("UID FETCH returned a duplicate UID")
        if require_payload and literal is None:
            raise ImapClientError("server returned no message payload")
        values[uid] = (head, literal)
    if set(values) != expected:
        raise ImapClientError("UID FETCH omitted a requested UID")
    return values


def _quote_mailbox(value: str) -> str:
    if not value or len(value) > 1024 or any(marker in value for marker in ("\r", "\n", "\x00")):
        raise ValueError("invalid mailbox name")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _structure_records(data: Iterable[Any], expected_uids: tuple[str, ...]) -> dict[str, MimePart]:
    """Parse one bounded BODYSTRUCTURE response per UID without MIME payloads."""
    records: dict[str, bytearray] = {}
    current: str | None = None
    for item in data:
        values = item if isinstance(item, tuple) else (item,)
        if not all(isinstance(value, bytes) for value in values):
            if item is not None:
                raise ImapClientError("server returned invalid BODYSTRUCTURE metadata")
            continue
        for value in values:
            match = _UID_RE.search(value)
            if match:
                uid = match.group("uid").decode("ascii")
                if uid not in expected_uids or uid in records:
                    raise ImapClientError("UID FETCH returned an unexpected or duplicate UID")
                records[uid] = bytearray()
                current = uid
            if current is None:
                raise ImapClientError("BODYSTRUCTURE response omitted UID framing")
            if len(records[current]) + len(value) > 2 * 1024 * 1024:
                raise ImapClientError("BODYSTRUCTURE response exceeded the retrieval limit")
            records[current].extend(value)
    if set(records) != set(expected_uids):
        raise ImapClientError("UID FETCH omitted a requested UID")
    try:
        return {uid: parse_bodystructure(bytes(raw)) for uid, raw in records.items()}
    except BodyStructureError as exc:
        raise ImapClientError("server returned invalid BODYSTRUCTURE") from exc


def _attachment_filenames(part: MimePart) -> Iterable[str]:
    # The outer message/rfc822 entity is itself an attachment (commonly an
    # .eml file), but its forwarded message must never influence attachment
    # filename search for the containing message.
    if part.filename:
        yield part.filename
    if part.content_type != "message/rfc822":
        for child in part.children:
            yield from _attachment_filenames(child)


def _mime_token(value: str, fallback: str) -> bytes:
    """Return a header token without accepting BODYSTRUCTURE control bytes."""
    return value.encode("ascii") if re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~/-]+", value) else fallback.encode("ascii")


def _mime_parameter(name: str, value: str) -> bytes | None:
    """Serialize one bounded MIME parameter for a synthetic entity."""
    if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", name) or any(
        marker in value for marker in ("\r", "\n", "\x00")
    ):
        return None
    encoded = value.replace("\\", "\\\\").replace('"', '\\"').encode("utf-8")
    return b"; " + name.encode("ascii") + b'=\"' + encoded + b'\"'


def _header_message(headers: bytes):
    """Parse a HEADER.FIELDS literal regardless of its line termination."""
    return parse_message(headers.rstrip(b"\r\n") + b"\r\n\r\n")


def _message_content(identity: MessageIdentity, raw: bytes, flags: tuple[str, ...]) -> MessageContent:
    """Convert a bounded full-message fallback without changing read behavior."""
    message = parse_message(raw)
    return MessageContent(
        identity=identity,
        headers=safe_headers(message),
        text=plain_text(message),
        flags=flags,
        attachments=attachment_metadata(message),
        source_size=len(raw),
    )
