from __future__ import annotations

import re
from email import policy
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser

from readndraft_imap_mcp.mime.html import sanitize_inbound_html

from readndraft_imap_mcp.imap.models import (
    AttachmentContent,
    AttachmentMetadata,
)

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_MESSAGE_BYTES = 50 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_HTML_BYTES = 2 * 1024 * 1024
SAFE_HEADERS = ("Date", "From", "To", "Cc", "Subject", "Message-ID", "In-Reply-To")


class MessageLimitError(ValueError):
    """Raised when a message or selected MIME part exceeds a read budget."""


class _HTMLToTextParser(HTMLParser):
    _hidden = {"head", "iframe", "math", "object", "script", "style", "svg", "template"}
    _blocks = {
        "blockquote", "div", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
        "p", "pre", "table", "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self._bytes = 0
        self._hidden_stack: list[str] = []
        self._lists: list[tuple[str, int]] = []

    def _append(self, value: str) -> None:
        size = len(value.encode("utf-8"))
        if self._bytes + size > MAX_TEXT_BYTES:
            raise MessageLimitError("plain-text body exceeds the 2 MB limit")
        self.output.append(value)
        self._bytes += size

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self._hidden:
            self._hidden_stack.append(tag)
            return
        if self._hidden_stack:
            return
        if tag in self._blocks:
            self._append("\n\n" if tag != "tr" else "\n")
        elif tag == "br":
            self._append("\n")
        elif tag in {"ul", "ol"}:
            self._lists.append((tag, 0))
            self._append("\n")
        elif tag == "li":
            prefix = "- "
            if self._lists and self._lists[-1][0] == "ol":
                kind, count = self._lists[-1]
                count += 1
                self._lists[-1] = (kind, count)
                prefix = f"{count}. "
            self._append("\n" + prefix)
        elif tag in {"td", "th"} and self.output and not self.output[-1].endswith(("\n", "| ")):
            self._append(" | ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() not in self._hidden:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._hidden_stack and tag == self._hidden_stack[-1]:
            self._hidden_stack.pop()
            return
        if self._hidden_stack:
            return
        if tag in {"ul", "ol"} and self._lists:
            self._lists.pop()
            self._append("\n")
        elif tag in self._blocks or tag == "li":
            self._append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden_stack:
            self._append(data)

    def text(self) -> str:
        lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in "".join(self.output).splitlines()]
        result: list[str] = []
        for line in lines:
            if line or (result and result[-1]):
                result.append(line)
        return "\n".join(result).strip()


def _body_leaf_parts(message: Message):
    """Yield body leaves while excluding attachments and related resources."""
    if message.get_content_disposition() == "attachment" or message.get_filename():
        return
    if not message.is_multipart():
        yield message
        return
    parts = list(message.iter_parts())
    if message.get_content_subtype() == "related" and parts:
        start = message.get_param("start")
        root = next((part for part in parts if start and part.get("Content-ID") == start), parts[0])
        yield from _body_leaf_parts(root)
        return
    for part in parts:
        yield from _body_leaf_parts(part)


def _body_value(message: Message, content_type: str) -> str | None:
    for part in _body_leaf_parts(message):
        if part.get_content_type() != content_type:
            continue
        value = part.get_content()
        if isinstance(value, str) and value.strip():
            return value
    return None


def sanitized_html(message: Message) -> str:
    value = _body_value(message, "text/html")
    if value is None:
        return ""
    if len(value.encode("utf-8")) > MAX_HTML_BYTES:
        raise MessageLimitError("HTML body exceeds the 2 MB limit")
    try:
        return sanitize_inbound_html(value, maximum_bytes=MAX_HTML_BYTES)
    except ValueError as exc:
        raise MessageLimitError(str(exc)) from exc


def sanitize_filename(filename: str | None) -> str:
    candidate = re.split(r"[\\/]", filename or "attachment")[-1]
    candidate = "".join(char for char in candidate if char >= " " and char != "\x7f")
    candidate = candidate.strip().strip(".")
    return (candidate or "attachment")[:255]


def parse_message(raw: bytes) -> Message:
    if len(raw) > MAX_MESSAGE_BYTES:
        raise MessageLimitError("message exceeds the 50 MB retrieval limit")
    return BytesParser(policy=policy.default).parsebytes(raw)


def safe_headers(message: Message) -> dict[str, str]:
    return {
        name.lower().replace("-", "_"): " ".join(str(message[name]).splitlines())[:4096]
        for name in SAFE_HEADERS
        if message[name] is not None
    }


def _leaf_parts(message: Message):
    for index, part in enumerate(message.walk(), start=1):
        if not part.is_multipart():
            yield f"part-{index}", part


def plain_text(message: Message) -> str:
    value = _body_value(message, "text/plain")
    if value is not None:
        if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
            raise MessageLimitError("plain-text body exceeds the 2 MB limit")
        return value
    html = _body_value(message, "text/html")
    if html is None:
        return ""
    parser = _HTMLToTextParser()
    parser.feed(html)
    parser.close()
    return parser.text()


def attachment_metadata(message: Message) -> tuple[AttachmentMetadata, ...]:
    result: list[AttachmentMetadata] = []
    for attachment_id, part in _leaf_parts(message):
        if part.get_content_disposition() != "attachment" and not part.get_filename():
            continue
        payload = part.get_payload(decode=True) or b""
        result.append(
            AttachmentMetadata(
                attachment_id=attachment_id,
                filename=sanitize_filename(part.get_filename()),
                content_type=part.get_content_type(),
                size=len(payload),
            )
        )
    return tuple(result)


def get_attachment(message: Message, attachment_id: str) -> AttachmentContent:
    for candidate_id, part in _leaf_parts(message):
        if candidate_id != attachment_id:
            continue
        if part.get_content_disposition() != "attachment" and not part.get_filename():
            break
        payload = part.get_payload(decode=True) or b""
        if len(payload) > MAX_ATTACHMENT_BYTES:
            raise MessageLimitError("attachment exceeds the 25 MB limit")
        metadata = AttachmentMetadata(
            attachment_id=candidate_id,
            filename=sanitize_filename(part.get_filename()),
            content_type=part.get_content_type(),
            size=len(payload),
        )
        return AttachmentContent(metadata=metadata, content=payload)
    raise KeyError("unknown attachment_id")

