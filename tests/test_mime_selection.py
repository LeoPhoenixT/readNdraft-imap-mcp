from __future__ import annotations

from readndraft_imap_mcp.imap.bodystructure import MimePart, parse_bodystructure
from readndraft_imap_mcp.imap.mime_selection import body_candidates, contains_attached_message, part_message
from readndraft_imap_mcp.mime.parser import get_attachment, plain_text, sanitize_filename


def test_selected_part_preserves_bodystructure_charset_without_top_headers() -> None:
    part = MimePart(
        "part-1",
        "1",
        "text/plain",
        "base64",
        8,
        None,
        None,
        None,
        content_type_params=(("charset", "iso-8859-1"),),
    )

    message = part_message(part, b"Y2Fm6Q==")

    assert plain_text(message) == "café"


def test_selected_attachment_uses_safe_filename_and_transfer_decoder() -> None:
    part = MimePart(
        "part-3",
        "2",
        "application/octet-stream",
        "base64",
        8,
        'report".txt\r\nX-Injected: yes',
        "attachment",
        None,
    )

    message = part_message(part, b"dGVzdA==", filename=part.filename)
    content = get_attachment(message, "part-1")

    assert content.content == b"test"
    assert content.metadata.filename == sanitize_filename(part.filename)
    assert message["X-Injected"] is None


def test_attached_multipart_does_not_become_message_body() -> None:
    root = parse_bodystructure(
        b'BODYSTRUCTURE (("TEXT" "PLAIN" NIL NIL NIL "7BIT" 4 1 NIL NIL NIL NIL) '
        b'(("TEXT" "PLAIN" NIL NIL NIL "7BIT" 8 1 NIL NIL NIL NIL) '
        b'"MIXED" NIL ("ATTACHMENT" ("FILENAME" "nested.mime"))) "MIXED")'
    )

    plain, html, attachments = body_candidates(root)

    assert [part.section for part in plain] == ["1"]
    assert not html
    assert [part.section for part in attachments] == ["2.1"]


def test_nested_attached_message_requires_full_message_fallback() -> None:
    root = parse_bodystructure(
        b'BODYSTRUCTURE (("TEXT" "PLAIN" NIL NIL NIL "7BIT" 4 1 NIL NIL NIL NIL) '
        b'("MESSAGE" "RFC822" NIL NIL NIL "7BIT" 123 '
        b'(NIL NIL NIL NIL NIL NIL NIL NIL NIL NIL) '
        b'("TEXT" "PLAIN" NIL NIL NIL "7BIT" 4 1 NIL NIL NIL NIL) 1 NIL NIL) "MIXED")'
    )

    assert contains_attached_message(root)
    assert not contains_attached_message(root.children[0])
    plain, html, attachments = body_candidates(root)
    assert [part.section for part in plain] == ["1"]
    assert not html and not attachments
