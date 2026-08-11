from __future__ import annotations

from email.message import EmailMessage
import random

import pytest

from readndraft_imap_mcp.mime.parser import (
    attachment_metadata,
    get_attachment,
    parse_message,
    plain_text,
    safe_headers,
    sanitize_filename,
    sanitized_html,
)


def build_message() -> EmailMessage:
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["To"] = "recipient@example.com"
    message["Subject"] = "Phase 2 test"
    message.set_content("safe plain text")
    message.add_alternative("<script>ignored()</script><p>HTML</p>", subtype="html")
    message.add_attachment(
        b"attachment bytes",
        maintype="application",
        subtype="octet-stream",
        filename="../../unsafe.txt",
    )
    return message


def test_plain_text_and_safe_headers_exclude_html() -> None:
    parsed = parse_message(build_message().as_bytes())
    assert plain_text(parsed).strip() == "safe plain text"
    assert "script" not in plain_text(parsed)
    assert safe_headers(parsed)["subject"] == "Phase 2 test"


def test_attachment_metadata_and_content_use_safe_filename() -> None:
    parsed = parse_message(build_message().as_bytes())
    attachments = attachment_metadata(parsed)
    assert len(attachments) == 1
    assert attachments[0].filename == "unsafe.txt"
    content = get_attachment(parsed, attachments[0].attachment_id)
    assert content.content == b"attachment bytes"
    assert content.metadata == attachments[0]


def test_filename_sanitization_handles_both_path_styles() -> None:
    assert sanitize_filename(r"..\..\windows.exe") == "windows.exe"
    assert sanitize_filename("../../unix.txt") == "unix.txt"


def test_unknown_attachment_id_is_rejected() -> None:
    parsed = parse_message(build_message().as_bytes())
    with pytest.raises(KeyError, match="attachment_id"):
        get_attachment(parsed, "part-999")


def test_html_is_allowlisted_and_active_content_is_removed() -> None:
    message = EmailMessage()
    message.set_content("plain")
    message.add_alternative(
        '<style>body{display:none}</style><script>steal()</script>'
        '<p onclick="steal()">Safe &amp; sound</p>'
        '<img src="https://tracker.example/pixel"><iframe>hidden</iframe>',
        subtype="html",
    )
    result = sanitized_html(parse_message(message.as_bytes()))
    assert result.strip() == "<p>Safe &amp; sound</p>"


@pytest.mark.parametrize(
    "hostile",
    (
        '<svg><script>alert(1)</script></svg><p>visible</p>',
        '<math><mtext><img src=x onerror=alert(1)></mtext></math><b>safe</b>',
        '<object data="https://attacker.example"><p>hidden</p></object><i>ok</i>',
        '<p style="background:url(https://tracker.example)" onclick="x()">text</p>',
        '<a href="javascript:alert(1)">link text</a><img src="cid:x">',
    ),
)
def test_html_hostile_corpus_removes_active_content_and_attributes(hostile) -> None:
    message = EmailMessage()
    message.set_content("plain")
    message.add_alternative(hostile, subtype="html")

    result = sanitized_html(parse_message(message.as_bytes()))

    lowered = result.casefold()
    for forbidden in ("script", "javascript", "onerror", "onclick", "style=", "src="):
        assert forbidden not in lowered


def test_malformed_mime_fuzz_corpus_stays_within_safe_outputs() -> None:
    generator = random.Random(8508)
    corpus = [bytes(generator.randrange(256) for _ in range(size)) for size in range(0, 2048, 41)]

    for raw in corpus:
        message = parse_message(raw)
        assert len(repr(safe_headers(message))) <= 40_000
        assert len(plain_text(message).encode("utf-8")) <= 2 * 1024 * 1024
        assert len(sanitized_html(message).encode("utf-8")) <= 2 * 1024 * 1024
        assert all("/" not in item.filename and "\\" not in item.filename for item in attachment_metadata(message))
