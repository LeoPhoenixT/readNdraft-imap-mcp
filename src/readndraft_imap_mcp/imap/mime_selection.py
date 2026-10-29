"""Pure selection and assembly of MIME entities from parsed BODYSTRUCTURE data."""

from __future__ import annotations

from email.message import Message

from readndraft_imap_mcp.mime.parser import parse_message, safe_headers, sanitize_filename

from .bodystructure import MimePart
from .models import AttachmentMetadata, MessageContent, MessageIdentity
from .parsing import _header_message, _mime_parameter, _mime_token


def body_candidates(root: MimePart) -> tuple[list[MimePart], list[MimePart], list[MimePart]]:
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


def part_message(part: MimePart, payload: bytes, *, filename: str | None = None) -> Message:
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


def contains_attached_message(part: MimePart) -> bool:
    """Whether partial reconstruction needs the bounded full-message fallback."""
    return part.content_type == "message/rfc822" or any(contains_attached_message(child) for child in part.children)


def selected_message_content(
    identity: MessageIdentity,
    headers: bytes,
    flags: tuple[str, ...],
    text: str,
    attachments: list[MimePart],
    source_size: int,
) -> MessageContent:
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
    return MessageContent(identity, safe_headers(_header_message(headers)), text, flags, metadata, source_size)
