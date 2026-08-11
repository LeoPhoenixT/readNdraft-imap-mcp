from __future__ import annotations

import re
from email import policy
from email.message import Message
from email.parser import BytesParser
from html import escape
from html.parser import HTMLParser

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


class _SanitizingHTMLParser(HTMLParser):
    _allowed = {
        "b", "blockquote", "br", "code", "div", "em", "hr", "i", "li",
        "ol", "p", "pre", "span", "strong", "table", "tbody", "td", "th",
        "thead", "tr", "u", "ul",
    }
    _blocked = {"embed", "head", "iframe", "math", "object", "script", "style", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self._blocked_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._blocked:
            self._blocked_depth += 1
        elif not self._blocked_depth and tag in self._allowed:
            self.output.append(f"<{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._blocked_depth and tag in self._allowed:
            self.output.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._blocked and self._blocked_depth:
            self._blocked_depth -= 1
        elif not self._blocked_depth and tag in self._allowed:
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._blocked_depth:
            self.output.append(escape(data))


def sanitized_html(message: Message) -> str:
    chunks: list[str] = []
    total = 0
    for _, part in _leaf_parts(message):
        if part.get_content_type() != "text/html":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        value = part.get_content()
        if not isinstance(value, str):
            continue
        total += len(value.encode("utf-8"))
        if total > MAX_HTML_BYTES:
            raise MessageLimitError("HTML body exceeds the 2 MB limit")
        parser = _SanitizingHTMLParser()
        parser.feed(value)
        parser.close()
        chunks.append("".join(parser.output))
    return "\n".join(chunks)


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
    chunks: list[str] = []
    total = 0
    for _, part in _leaf_parts(message):
        if part.get_content_type() != "text/plain":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        value = part.get_content()
        if not isinstance(value, str):
            continue
        total += len(value.encode("utf-8"))
        if total > MAX_TEXT_BYTES:
            raise MessageLimitError("plain-text body exceeds the 2 MB limit")
        chunks.append(value)
    return "\n".join(chunks)


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

