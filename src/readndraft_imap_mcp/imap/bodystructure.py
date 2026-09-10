"""Small, fail-closed RFC 3501 BODYSTRUCTURE reader.

The IMAP grammar is deliberately parsed here instead of using a MIME parser:
BODYSTRUCTURE is an IMAP S-expression whose quoted values and literals are not
RFC 5322 header values.  This module only exposes the information required to
avoid downloading unrelated MIME bodies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from urllib.parse import unquote_to_bytes


class BodyStructureError(ValueError):
    pass


MAX_BODYSTRUCTURE_BYTES = 2 * 1024 * 1024
MAX_BODYSTRUCTURE_DEPTH = 64
MAX_BODYSTRUCTURE_TOKENS = 10_000


@dataclass(frozen=True, slots=True)
class MimePart:
    part_id: str
    section: str | None
    content_type: str
    transfer_encoding: str | None
    encoded_size: int
    filename: str | None
    disposition: str | None
    content_id: str | None
    related_start: str | None = None
    children: tuple["MimePart", ...] = ()
    content_type_params: tuple[tuple[str, str], ...] = ()

    @property
    def is_attachment(self) -> bool:
        return self.disposition == "attachment" or self.filename is not None


def _tokens(raw: bytes) -> list[bytes | None | str]:
    if len(raw) > MAX_BODYSTRUCTURE_BYTES:
        raise BodyStructureError("BODYSTRUCTURE exceeds the retrieval limit")
    out: list[bytes | None | str] = []
    i = 0
    while i < len(raw):
        if raw[i] in b" \t\r\n":
            i += 1
            continue
        if raw[i] in b"()":
            out.append(chr(raw[i]))
            i += 1
            continue
        if raw[i : i + 3].upper() == b"NIL" and (i + 3 == len(raw) or raw[i + 3] in b" ()\r\n"):
            out.append(None)
            i += 3
            continue
        if raw[i] == 34:
            i += 1
            value = bytearray()
            while i < len(raw) and raw[i] != 34:
                if raw[i] == 92 and i + 1 < len(raw):
                    i += 1
                value.append(raw[i])
                i += 1
            if i == len(raw):
                raise BodyStructureError("unterminated quoted BODYSTRUCTURE string")
            out.append(bytes(value))
            i += 1
            continue
        if raw[i] == 123:
            end = _literal_end(raw, i)
            marker = raw.find(b"}", i)
            literal_start = marker + 1
            literal_start += 2 if raw[literal_start : literal_start + 2] == b"\r\n" else 1
            out.append(raw[literal_start:end])
            i = end
            continue
        end = i
        while end < len(raw) and raw[end] not in b" ()\t\r\n":
            end += 1
        out.append(raw[i:end])
        i = end
        if len(out) > MAX_BODYSTRUCTURE_TOKENS:
            raise BodyStructureError("BODYSTRUCTURE has too many values")
        continue
    if len(out) > MAX_BODYSTRUCTURE_TOKENS:
        raise BodyStructureError("BODYSTRUCTURE has too many values")
    return out


def _tree(tokens: list[bytes | None | str]) -> object:
    stack: list[list[object]] = []
    root: object | None = None
    for value in tokens:
        if value == "(":
            stack.append([])
            if len(stack) > MAX_BODYSTRUCTURE_DEPTH:
                raise BodyStructureError("BODYSTRUCTURE nesting is too deep")
        elif value == ")":
            if not stack:
                raise BodyStructureError("unbalanced BODYSTRUCTURE")
            item = stack.pop()
            if stack:
                stack[-1].append(item)
            elif root is None:
                root = item
            else:
                raise BodyStructureError("multiple BODYSTRUCTURE values")
        else:
            if not stack:
                raise BodyStructureError("BODYSTRUCTURE value outside list")
            stack[-1].append(value)
    if stack or not isinstance(root, list):
        raise BodyStructureError("incomplete BODYSTRUCTURE")
    return root


def _text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, bytes):
        raise BodyStructureError("invalid BODYSTRUCTURE atom")
    return value.decode("utf-8", errors="replace")


def _params(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, list) or len(value) % 2:
        raise BodyStructureError("invalid BODYSTRUCTURE parameters")
    result: dict[str, str] = {}
    for key, item in zip(value[::2], value[1::2], strict=True):
        name, content = _text(key), _text(item)
        if name is None or content is None:
            raise BodyStructureError("invalid BODYSTRUCTURE parameter")
        result[name.casefold()] = content
    return result


def _disposition(value: object) -> tuple[str | None, dict[str, str]]:
    if value is None:
        return None, {}
    if not isinstance(value, list) or len(value) != 2:
        raise BodyStructureError("invalid BODYSTRUCTURE disposition")
    return (_text(value[0]) or "").casefold() or None, _params(value[1])


def _filename(params: dict[str, str]) -> str | None:
    """Return a conservative RFC 2231 filename without accepting gaps."""
    direct = params.get("filename") or params.get("name")
    extended = params.get("filename*") or params.get("name*")
    if extended:
        charset, _, encoded = extended.partition("'")
        _, _, encoded = encoded.partition("'")
        try:
            return unquote_to_bytes(encoded).decode(charset or "utf-8", errors="replace")
        except LookupError:
            return unquote_to_bytes(encoded).decode("utf-8", errors="replace")
    for prefix in ("filename", "name"):
        chunks: list[str] = []
        encoded = False
        index = 0
        while f"{prefix}*{index}" in params or f"{prefix}*{index}*" in params:
            key = f"{prefix}*{index}*" if f"{prefix}*{index}*" in params else f"{prefix}*{index}"
            chunks.append(params[key])
            encoded = encoded or key.endswith("*")
            index += 1
        if chunks:
            value = "".join(chunks)
            if encoded:
                if "''" not in chunks[0]:
                    value = "utf-8''" + value
                return _filename({f"{prefix}*": value})
            return value
    return direct


def _number(value: object) -> int:
    raw = _text(value)
    if raw is None or not re.fullmatch(r"[0-9]+", raw):
        raise BodyStructureError("invalid BODYSTRUCTURE size")
    return int(raw)


def _message_envelope(value: object) -> None:
    """Validate the fixed-width ENVELOPE field of a message/rfc822 part."""
    if not isinstance(value, list) or len(value) != 10:
        raise BodyStructureError("invalid BODYSTRUCTURE message envelope")


def _message_body(value: object) -> None:
    """Check that the embedded body is structurally present without using it.

    A selected message/rfc822 section needs full-message parsing to preserve
    the forwarded message faithfully, so callers deliberately do not descend
    into this tree.  Its presence is still validated here to reject malformed
    BODYSTRUCTURE values before trusting outer attachment metadata.
    """
    if not isinstance(value, list) or not value:
        raise BodyStructureError("invalid BODYSTRUCTURE embedded message body")


def _parse(value: object, section: str | None, counter: list[int]) -> MimePart:
    if not isinstance(value, list) or not value:
        raise BodyStructureError("invalid BODYSTRUCTURE part")
    counter[0] += 1
    part_id = f"part-{counter[0]}"
    if isinstance(value[0], list):
        child_values: list[object] = []
        index = 0
        while index < len(value) and isinstance(value[index], list):
            child_values.append(value[index])
            index += 1
        if index >= len(value):
            raise BodyStructureError("multipart missing subtype")
        subtype = (_text(value[index]) or "").casefold()
        # Multipart extensions are parameter, disposition, language, location.
        multipart_params = _params(value[index + 1]) if len(value) > index + 1 else {}
        disposition, disp_params = _disposition(value[index + 2]) if len(value) > index + 2 else (None, {})
        children = tuple(
            _parse(child, f"{section}.{n}" if section else str(n), counter) for n, child in enumerate(child_values, 1)
        )
        return MimePart(
            part_id,
            section,
            f"multipart/{subtype}",
            None,
            0,
            _filename(disp_params),
            disposition,
            None,
            multipart_params.get("start"),
            children,
            tuple(multipart_params.items()),
        )
    if len(value) < 7:
        raise BodyStructureError("short BODYSTRUCTURE part")
    major, minor = _text(value[0]), _text(value[1])
    if major is None or minor is None:
        raise BodyStructureError("missing BODYSTRUCTURE type")
    params = _params(value[2])
    encoding = _text(value[5])
    size = _number(value[6])
    major_folded = major.casefold()
    minor_folded = minor.casefold()
    if major_folded == "message" and minor_folded == "rfc822":
        # RFC 3501 MESSAGE/RFC822 fields add envelope, embedded body and line
        # count before md5/disposition.  Do not descend into the embedded
        # message: read paths intentionally use the bounded full fallback,
        # while the outer disposition remains searchable as an .eml file.
        if len(value) < 10:
            raise BodyStructureError("short BODYSTRUCTURE message/rfc822 part")
        _message_envelope(value[7])
        _message_body(value[8])
        _number(value[9])
        disposition_index = 11
    else:
        # Disposition follows md5: index 9 for text parts and index 8 for
        # basic parts.
        disposition_index = 9 if major_folded == "text" else 8
    disposition, disp_params = _disposition(value[disposition_index]) if len(value) > disposition_index else (None, {})
    filename = _filename({**params, **disp_params})
    content_id = _text(value[3])
    return MimePart(
        part_id,
        section,
        f"{major_folded}/{minor_folded}",
        encoding,
        size,
        filename,
        disposition,
        content_id,
        None,
        (),
        tuple(params.items()),
    )


def parse_bodystructure(raw: bytes) -> MimePart:
    """Parse the BODYSTRUCTURE expression, including quoted strings/literals."""
    marker = _bodystructure_marker(raw)
    if marker is None:
        raise BodyStructureError("server omitted BODYSTRUCTURE")
    start = raw.find(b"(", marker)
    if start < 0:
        raise BodyStructureError("invalid BODYSTRUCTURE")
    # The tokenizer rejects trailing FETCH attributes, so retain only one
    # literal-aware balanced expression.  A literal may legitimately contain
    # quotes or parentheses.
    end = _expression_end(raw, start)
    root = _parse(_tree(_tokens(raw[start:end])), None, [0])
    # A multipart BODYSTRUCTURE has no section of its own, but a non-multipart
    # message root is addressed as section 1 (RFC 3501, 6.4.5).  Preserve the
    # public part id while making that body selectable through BODY.PEEK[1].
    return replace(root, section="1") if not root.content_type.startswith("multipart/") else root


def _literal_end(raw: bytes, start: int) -> int:
    """Return the first byte after an IMAP literal, with strict framing."""
    literal_end = raw.find(b"}", start)
    if literal_end < 0:
        raise BodyStructureError("invalid BODYSTRUCTURE literal")
    length_text = raw[start + 1 : literal_end]
    # RFC 7888 non-synchronising literals append '+'.  They carry exactly the
    # same bytes after the CRLF and are harmless to accept here.
    if length_text.endswith(b"+"):
        length_text = length_text[:-1]
    if not length_text or not length_text.isdigit():
        raise BodyStructureError("invalid BODYSTRUCTURE literal")
    length = int(length_text)
    literal_start = literal_end + 1
    if raw[literal_start : literal_start + 2] == b"\r\n":
        literal_start += 2
    elif raw[literal_start : literal_start + 1] == b"\n":
        literal_start += 1
    else:
        raise BodyStructureError("invalid BODYSTRUCTURE literal")
    end = literal_start + length
    if end > len(raw):
        raise BodyStructureError("truncated BODYSTRUCTURE literal")
    return end


def _expression_end(raw: bytes, start: int) -> int:
    """Locate one balanced expression while respecting quoted/literal bytes."""
    depth = 0
    quoted = False
    escaped = False
    i = start
    while i < len(raw):
        byte = raw[i]
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
            i += 1
            continue
        if byte == 34:
            quoted = True
        elif byte == 123:
            i = _literal_end(raw, i)
            continue
        elif byte == 40:
            depth += 1
        elif byte == 41:
            depth -= 1
            if depth == 0:
                return i + 1
            if depth < 0:
                raise BodyStructureError("unbalanced BODYSTRUCTURE")
        i += 1
    if quoted:
        raise BodyStructureError("unterminated quoted BODYSTRUCTURE string")
    if depth:
        raise BodyStructureError("unterminated BODYSTRUCTURE")
    raise BodyStructureError("invalid BODYSTRUCTURE")


def _bodystructure_marker(raw: bytes) -> int | None:
    """Find a FETCH BODYSTRUCTURE attribute, never text inside a literal."""
    upper = raw.upper()
    quoted = False
    escaped = False
    i = 0
    while i < len(raw):
        byte = raw[i]
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
            i += 1
            continue
        if byte == 34:
            quoted = True
            i += 1
            continue
        if byte == 123:
            i = _literal_end(raw, i)
            continue
        if upper.startswith(b"BODYSTRUCTURE", i):
            before = raw[i - 1 : i]
            after = raw[i + 13 : i + 14]
            if (not before or before in b" (\r\n") and after in b" (\r\n":
                return i
        i += 1
    return None
