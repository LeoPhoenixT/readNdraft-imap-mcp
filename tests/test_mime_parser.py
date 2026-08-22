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
    message["Subject"] = "MIME parser test"
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
    assert safe_headers(parsed)["subject"] == "MIME parser test"


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


def test_inbound_html_preserves_safe_links_tables_and_inline_css() -> None:
    message = EmailMessage()
    message.set_content("plain")
    message.add_alternative(
        '<p style="color:#123456; padding:8px; position:fixed">See '
        '<a href="https://example.com/report">report</a>.</p>'
        '<table role="presentation" width="100%" style="border-collapse:collapse">'
        '<tr><td style="border:1px solid #333; text-align:center">Done</td></tr></table>',
        subtype="html",
    )

    result = sanitized_html(parse_message(message.as_bytes()))

    assert 'href="https://example.com/report"' in result
    assert 'rel="noopener noreferrer"' in result
    assert 'style="color:#123456;padding:8px"' in result
    assert "position" not in result
    assert 'role="presentation"' in result
    assert 'width="100%"' in result
    assert "border-collapse:collapse" in result
    assert "border:1px solid #333" in result


def test_inbound_html_strips_unsafe_links_images_and_stylesheets() -> None:
    message = EmailMessage()
    message.set_content("plain")
    message.add_alternative(
        '<link rel="stylesheet" href="https://tracker.example/x.css">'
        '<a href="java&#x0a;script:alert(1)">bad</a>'
        '<img src="https://tracker.example/pixel"><p>Visible</p>',
        subtype="html",
    )

    result = sanitized_html(parse_message(message.as_bytes()))

    assert "tracker.example" not in result
    assert "href=" not in result
    assert "img" not in result
    assert "Visible" in result


def test_html_only_message_has_structured_plain_text_fallback() -> None:
    message = EmailMessage()
    message.set_content(
        "<h2>Report</h2><p>Hello</p><p>World</p>"
        "<ol><li>First</li><li>Second</li></ol>"
        "<table><tr><th>Name</th><th>Status</th></tr>"
        "<tr><td>Leo</td><td>Complete</td></tr></table>",
        subtype="html",
    )

    result = plain_text(parse_message(message.as_bytes()))

    assert "Report" in result
    assert "Hello\n\nWorld" in result
    assert "1. First" in result
    assert "2. Second" in result
    assert "Name | Status" in result
    assert "Leo | Complete" in result


def test_plain_alternative_is_preferred_over_html() -> None:
    message = EmailMessage()
    message.set_content("preferred plain")
    message.add_alternative("<p>HTML equivalent</p>", subtype="html")

    assert plain_text(parse_message(message.as_bytes())).strip() == "preferred plain"


def test_whitespace_plain_alternative_falls_back_to_html() -> None:
    message = EmailMessage()
    message.set_content("   \n")
    message.add_alternative("<p>Readable HTML</p>", subtype="html")

    assert plain_text(parse_message(message.as_bytes())) == "Readable HTML"


def test_attached_message_bodies_are_not_parent_body_content() -> None:
    attached = EmailMessage()
    attached.set_content("secret attached plain")
    attached.add_alternative("<p>secret attached HTML</p>", subtype="html")
    parent = EmailMessage()
    parent.set_content("parent body")
    parent.add_attachment(attached)

    parsed = parse_message(parent.as_bytes())
    assert plain_text(parsed).strip() == "parent body"
    assert sanitized_html(parsed) == ""


def test_related_uses_root_html_and_ignores_related_resources() -> None:
    message = EmailMessage()
    message.set_content("<p>Root body</p>", subtype="html")
    message.make_related()
    resource = EmailMessage()
    resource.set_content("not body text")
    resource["Content-ID"] = "<resource>"
    message.attach(resource)

    parsed = parse_message(message.as_bytes())
    assert plain_text(parsed) == "Root body"
    assert sanitized_html(parsed).strip() == "<p>Root body</p>"


def test_hidden_html_content_is_not_emitted_in_plain_fallback() -> None:
    message = EmailMessage()
    message.set_content(
        "<script>secret()</script><style>.hidden{}</style>"
        "<iframe>hidden frame</iframe><p>Visible</p>",
        subtype="html",
    )

    assert plain_text(parse_message(message.as_bytes())) == "Visible"


def test_mismatched_hidden_tags_do_not_expose_active_content() -> None:
    message = EmailMessage()
    message.set_content(
        "<script>first</style>still hidden</script><p>Visible</p>",
        subtype="html",
    )

    parsed = parse_message(message.as_bytes())
    assert plain_text(parsed) == "Visible"
    assert sanitized_html(parsed).strip() == "<p>Visible</p>"


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
