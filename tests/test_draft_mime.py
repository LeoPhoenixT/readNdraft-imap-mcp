from __future__ import annotations

from email import policy
from email.parser import BytesParser

import pytest

from readndraft_imap_mcp.mime.drafts import (
    DraftAttachment,
    PreparedDraft,
    build_draft_message,
    prepare_draft,
)
from readndraft_imap_mcp.mime.html import prepare_html


def test_generated_draft_preserves_client_editable_fields_and_attachment(tmp_path) -> None:
    attachment = (tmp_path / "report.txt").resolve()
    attachment.write_text("attachment body", encoding="utf-8")
    draft = prepare_draft(
        to=("to@example.com",),
        cc=("cc@example.com",),
        bcc=("bcc@example.com",),
        subject="Client compatibility",
        body="editable body",
        attachments=(DraftAttachment("report.txt", 15, "a" * 64, b"attachment body"),),
    )
    raw, message_id = build_draft_message("owner@example.com", draft)
    message = BytesParser(policy=policy.default).parsebytes(raw)

    assert b"\r\n" in raw
    assert message["From"] == "owner@example.com"
    assert message["To"] == "to@example.com"
    assert message["Cc"] == "cc@example.com"
    assert message["Bcc"] == "bcc@example.com"
    assert message["Subject"] == "Client compatibility"
    assert message["Message-ID"] == message_id
    attachment_part = next(message.iter_attachments())
    assert attachment_part.get_filename() == "report.txt"
    assert attachment_part.get_content().strip() == "attachment body"
    assert len(draft.attachments[0].sha256) == 64


def test_generated_draft_allows_no_recipients() -> None:
    draft = prepare_draft(to=(), subject="Unaddressed", body="editable body")
    raw, _ = build_draft_message("owner@example.com", draft)
    message = BytesParser(policy=policy.default).parsebytes(raw)

    assert str(message["To"]) == ""
    assert message["Cc"] is None
    assert message["Bcc"] is None


def test_plain_draft_structure_is_unchanged() -> None:
    draft = prepare_draft(to=("to@example.com",), subject="Plain", body="plain body")
    raw, _ = build_draft_message("owner@example.com", draft)
    message = BytesParser(policy=policy.default).parsebytes(raw)

    assert message.get_content_type() == "text/plain"
    assert message.get_content().strip() == "plain body"


def test_html_draft_is_ordered_multipart_alternative_and_round_trips_utf8() -> None:
    draft = prepare_draft(
        to=("to@example.com",),
        subject="報告 日本語 📬",
        body="你好\n日本語 📬",
        html_body="<p>你好</p><p><strong>日本語 📬</strong></p>",
    )
    raw, _ = build_draft_message("owner@example.com", draft)
    message = BytesParser(policy=policy.default).parsebytes(raw)

    assert message.get_content_type() == "multipart/alternative"
    parts = list(message.iter_parts())
    assert [part.get_content_type() for part in parts] == ["text/plain", "text/html"]
    assert parts[0].get_content().splitlines() == draft.body.splitlines()
    assert parts[1].get_content().strip() == draft.html_body
    assert str(message["Subject"]) == draft.subject


def test_html_draft_with_attachment_uses_mixed_alternative_tree() -> None:
    attachment = DraftAttachment("資料.txt", 4, "a" * 64, b"data")
    draft = prepare_draft(
        to=("to@example.com",), subject="Rich", body="plain",
        html_body="<p><b>rich</b></p>", attachments=(attachment,),
    )
    raw, _ = build_draft_message("owner@example.com", draft)
    message = BytesParser(policy=policy.default).parsebytes(raw)

    assert message.get_content_type() == "multipart/mixed"
    parts = list(message.iter_parts())
    assert parts[0].get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in parts[0].iter_parts()] == ["text/plain", "text/html"]
    assert parts[1].get_filename() == "資料.txt"
    assert parts[1].get_payload(decode=True) == b"data"


def test_prepared_draft_positional_construction_remains_compatible() -> None:
    draft = PreparedDraft(("to@example.com",), (), (), "subject", "body", ())
    assert draft.html_body is None


@pytest.mark.parametrize(
    "html",
    [
        "<script>alert(1)</script>",
        "<iframe src='https://example.com'></iframe>",
        "<p onclick='alert(1)'>unsafe</p>",
        "<a href='javascript:alert(1)'>unsafe</a>",
        "<a href='jAvAsCrIpT&#58;alert(1)'>unsafe</a>",
        "<a href='//example.com'>unsafe</a>",
        "<img src='https://example.com/tracker.png'>",
    ],
)
def test_draft_rejects_unsafe_or_unsupported_html(html: str) -> None:
    with pytest.raises(ValueError):
        prepare_draft(to=("to@example.com",), subject="safe", body="plain", html_body=html)


def test_draft_accepts_common_structural_html() -> None:
    html = (
        "<!doctype html><html><body><h1>Report</h1><p>See "
        "<a href='https://example.com' title='report'>details</a>.</p>"
        "<blockquote><em>Note</em></blockquote><ol><li>One</li></ol>"
        "<table role='presentation' width='100%'><tbody><tr><td colspan='2' align='center'>Cell</td>"
        "</tr></tbody></table></body></html>"
    )
    prepared = prepare_draft(
        to=("to@example.com",), subject="safe", body="plain", html_body=html
    ).html_body
    assert prepared is not None
    assert prepared.startswith("<!DOCTYPE html><html><head></head><body>")
    assert '<a href="https://example.com"' in prepared
    assert '<table role="presentation" width="100%">' in prepared


def test_draft_inlines_safe_email_css_and_preserves_layout() -> None:
    html = """<!doctype html><html><head><style>
        .card { color: #123456; background-color: #eeeeee; padding: 12px;
                border: 1px solid #333333; max-width: 600px; }
        table { border-collapse: collapse; border-spacing: 0; }
        td { border: 1px solid #999999; padding: 6px; text-align: center; }
    </style></head><body><div class="card">Hello</div>
    <table><tr><td>Cell</td></tr></table></body></html>"""

    prepared = prepare_draft(
        to=("to@example.com",), subject="CSS", body="Hello\nCell", html_body=html
    ).html_body

    assert prepared is not None
    assert "<style" not in prepared
    assert "class=" not in prepared
    assert "padding:12px" in prepared
    assert "border:1px solid #333333" in prepared
    assert "border-collapse:collapse" in prepared
    assert "border-spacing:0" in prepared
    assert "text-align:center" in prepared


@pytest.mark.parametrize(
    "css",
    [
        "p { background-color: red; background-image: url(https://tracker.example/x); }",
        "p { border: url(https://tracker.example/x); }",
        "@import url(https://tracker.example/x); p { color: red; }",
        "p { color: var(--secret); }",
        "p { position: fixed; z-index: 999; }",
        "p { display: none; }",
        "p { animation: pulse 1s; }",
    ],
)
def test_draft_rejects_unsafe_or_unsupported_css(css: str) -> None:
    with pytest.raises(ValueError):
        prepare_draft(
            to=("to@example.com",), subject="CSS", body="plain",
            html_body=f"<html><head><style>{css}</style></head><body><p>x</p></body></html>",
        )


def test_external_stylesheet_is_rejected_without_network(monkeypatch) -> None:
    import socket

    def fail_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    with pytest.raises(ValueError, match="link"):
        prepare_draft(
            to=("to@example.com",), subject="CSS", body="plain",
            html_body=("<html><head><link rel='stylesheet' href='https://tracker.example/x.css'>"
                       "</head><body><p>x</p></body></html>"),
        )


def test_realistic_outlook_gmail_style_fixture_is_normalized() -> None:
    html = """<!doctype html><html><head><title>Status</title><style>
      body { margin: 0; padding: 0; font-family: Arial, sans-serif; color: #202124; }
      .container { width: 100%; max-width: 600px; border-collapse: collapse; }
      .cell { padding: 16px; border: 1px solid #dadce0; line-height: 1.5; }
      .button { display: inline-block; background-color: #1a73e8; color: #ffffff;
                padding: 10px 16px; text-decoration: none; border-radius: 4px; }
    </style></head><body><!--[if mso]>Outlook metadata<![endif]-->
      <table role="presentation" class="container" cellpadding="0" cellspacing="0">
        <tr><td class="cell"><h1>Status update</h1><p>Everything is ready.</p>
        <a class="button" href="https://example.com/status">View status</a></td></tr>
      </table></body></html>"""

    prepared = prepare_draft(
        to=("to@example.com",), subject="Status", body="Status update\nEverything is ready.",
        html_body=html,
    ).html_body

    assert prepared is not None
    assert "Outlook metadata" not in prepared
    assert "font-family:Arial, sans-serif" in prepared
    assert "max-width:600px" in prepared
    assert "display:inline-block" in prepared
    assert 'href="https://example.com/status"' in prepared
    assert "class=" not in prepared


def test_malformed_but_recoverable_html_is_deterministically_normalized() -> None:
    prepared = prepare_draft(
        to=("to@example.com",), subject="Malformed", body="One\nTwo",
        html_body="<html><body><table><tr><td>One<td><strong>Two</table></body>",
    ).html_body

    assert prepared is not None
    assert prepared.startswith("<!DOCTYPE html>")
    assert "One" in prepared and "<strong>Two</strong>" in prepared


def test_draft_enforces_plain_and_html_limits(monkeypatch) -> None:
    import readndraft_imap_mcp.mime.drafts as drafts

    monkeypatch.setattr(drafts, "MAX_TEXT_BODY_BYTES", 3)
    with pytest.raises(ValueError, match="draft body"):
        prepare_draft(to=(), subject="safe", body="four")
    monkeypatch.setattr(drafts, "MAX_HTML_BODY_BYTES", 3)
    with pytest.raises(ValueError, match="HTML body"):
        prepare_draft(to=(), subject="safe", body="ok", html_body="<p>x</p>")


def test_draft_enforces_final_serialized_mime_limit(monkeypatch) -> None:
    import readndraft_imap_mcp.mime.drafts as drafts

    draft = prepare_draft(to=(), subject="safe", body="body", html_body="<p>body</p>")
    monkeypatch.setattr(drafts, "MAX_MESSAGE_BYTES", 10)
    with pytest.raises(ValueError, match="generated draft"):
        build_draft_message("owner@example.com", draft)


def test_prepared_html_enforces_post_normalization_limit() -> None:
    with pytest.raises(ValueError, match="prepared draft HTML"):
        prepare_html("<p>x</p>", maximum_bytes=20)


def test_draft_headers_reject_injection() -> None:
    with pytest.raises(ValueError, match="subject"):
        prepare_draft(
            to=("to@example.com",),
            subject="safe\r\nBcc: attacker@example.com",
            body="body",
        )


def test_draft_rejects_duplicate_attachment_names() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        prepare_draft(
            to=("to@example.com",),
            subject="safe",
            body="body",
            attachments=(
                DraftAttachment("same.txt", 4, "a" * 64, b"same"),
                DraftAttachment("same.txt", 4, "a" * 64, b"same"),
            ),
        )


def test_draft_rejects_too_many_attachment_entries(tmp_path) -> None:
    with pytest.raises(ValueError, match="25 attachment"):
        prepare_draft(
            to=("to@example.com",),
            subject="safe",
            body="body",
            attachments=tuple(
                DraftAttachment(f"{index}.txt", 0, "a" * 64, b"")
                for index in range(26)
            ),
        )
