from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid

from .html import prepare_authored_html
from .parser import MAX_MESSAGE_BYTES

MAX_RECIPIENTS = 100
MAX_SUBJECT_CHARS = 998
MAX_TEXT_BODY_BYTES = 2 * 1024 * 1024
MAX_HTML_BODY_BYTES = 2 * 1024 * 1024
MAX_ATTACHMENTS = 25

@dataclass(frozen=True, slots=True)
class DraftAttachment:
    filename: str
    size: int
    sha256: str
    content: bytes


@dataclass(frozen=True, slots=True)
class PreparedDraft:
    to: tuple[Address, ...]
    cc: tuple[Address, ...]
    bcc: tuple[Address, ...]
    subject: str
    body: str
    attachments: tuple[DraftAttachment, ...]
    html_body: str | None = None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()


def _validate_addresses(values: tuple[str, ...], field: str) -> tuple[Address, ...]:
    if any("\r" in value or "\n" in value for value in values):
        raise ValueError(f"{field} addresses must not contain line breaks")
    addresses: list[Address] = []
    for value in values:
        # getaddresses deliberately expands lists/groups. One API entry must map
        # to one mailbox so callers cannot bypass the recipient limit.
        in_quote = False
        escaped = False
        group_syntax = False
        for char in value:
            if escaped:
                escaped = False
            elif char == "\\" and in_quote:
                escaped = True
            elif char == '"':
                in_quote = not in_quote
            elif not in_quote and char in ":;":
                group_syntax = True
        if group_syntax or "<@" in value:
            raise ValueError(f"invalid {field} address")
        parsed = getaddresses([value], strict=True)
        if len(parsed) != 1:
            raise ValueError(f"invalid {field} address")
        name, addr_spec = parsed[0]
        if not addr_spec or addr_spec.count("@") != 1 or ":" in addr_spec.split("@", 1)[0]:
            raise ValueError(f"invalid {field} address")
        try:
            addresses.append(Address(display_name=name, addr_spec=addr_spec))
        except ValueError as exc:
            raise ValueError(f"invalid {field} address") from exc
    return tuple(addresses)


def prepare_draft(
    *,
    to: tuple[str, ...],
    cc: tuple[str, ...] = (),
    bcc: tuple[str, ...] = (),
    subject: str,
    body: str,
    attachments: tuple[DraftAttachment, ...] = (),
    html_body: str | None = None,
    in_reply_to: str | None = None,
    references: tuple[str, ...] = (),
) -> PreparedDraft:
    recipients = len(to) + len(cc) + len(bcc)
    if recipients > MAX_RECIPIENTS:
        raise ValueError("draft exceeds the 100 recipient limit")
    if "\r" in subject or "\n" in subject or len(subject) > MAX_SUBJECT_CHARS:
        raise ValueError("invalid draft subject")
    if len(body.encode("utf-8")) > MAX_TEXT_BODY_BYTES:
        raise ValueError("draft body exceeds the 2 MB limit")
    if html_body is not None:
        html_body = prepare_authored_html(html_body, maximum_bytes=MAX_HTML_BODY_BYTES)
    if len(attachments) > MAX_ATTACHMENTS:
        raise ValueError("draft exceeds the 25 attachment limit")
    if len({item.filename for item in attachments}) != len(attachments):
        raise ValueError("duplicate attachment name")
    if sum(item.size for item in attachments) > MAX_MESSAGE_BYTES:
        raise ValueError("attachments exceed the 50 MB total limit")
    return PreparedDraft(
        to=_validate_addresses(to, "To"),
        cc=_validate_addresses(cc, "Cc"),
        bcc=_validate_addresses(bcc, "Bcc"),
        subject=subject,
        body=body,
        attachments=attachments,
        html_body=html_body,
        in_reply_to=in_reply_to,
        references=references,
    )


def build_draft_message(
    from_address: str,
    draft: PreparedDraft,
    *,
    sender_name: str | None = None,
    message_id: str | None = None,
) -> tuple[bytes, str]:
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = Address(display_name=sender_name or "", addr_spec=from_address)
    message["To"] = draft.to if draft.to else ""
    if draft.cc:
        message["Cc"] = draft.cc
    if draft.bcc:
        message["Bcc"] = draft.bcc
    message["Subject"] = draft.subject
    message["Date"] = formatdate(localtime=True)
    message_id = message_id or make_msgid(domain=from_address.partition("@")[2] or None)
    message["Message-ID"] = message_id
    if draft.in_reply_to is not None:
        message["In-Reply-To"] = draft.in_reply_to
        message["References"] = " ".join(draft.references)
    message.set_content(draft.body)
    if draft.html_body is not None:
        message.add_alternative(draft.html_body, subtype="html")
    for attachment in draft.attachments:
        guessed, _ = mimetypes.guess_type(attachment.filename)
        maintype, subtype = (guessed or "application/octet-stream").split("/", 1)
        message.add_attachment(
            attachment.content,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
        )
    raw = message.as_bytes(policy=policy.SMTP)
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("generated draft exceeds the 50 MB limit")
    return raw, message_id
