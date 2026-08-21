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


@pytest.mark.parametrize("sender_name", ["Display Name", "山田太郎"])
def test_sender_display_name_uses_structured_from_header(sender_name: str) -> None:
    draft = prepare_draft(to=("reader@example.com",), subject="Hello", body="Body")
    raw, _ = build_draft_message(
        "user@example.com", draft, sender_name=sender_name
    )
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    assert parsed["From"].addresses[0].display_name == sender_name
    assert parsed["From"].addresses[0].addr_spec == "user@example.com"


def test_sender_without_display_name_remains_address_only() -> None:
    draft = prepare_draft(to=("reader@example.com",), subject="Hello", body="Body")
    raw, _ = build_draft_message("user@example.com", draft)
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    assert parsed["From"].addresses[0].display_name == ""
    assert parsed["From"].addresses[0].addr_spec == "user@example.com"
from readndraft_imap_mcp.mime.html import (
    AUTHORED_POLICY,
    AuthoredHtmlError,
    INBOUND_POLICY,
    prepare_html,
    sanitize_inbound_html,
)


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


def test_named_recipients_are_preserved_as_mailboxes() -> None:
    draft = prepare_draft(
        to=("\"Dr: Ada\" <ada@example.com>", "山田太郎 <taro@example.jp>"),
        cc=("cc@example.com",), bcc=("Private <bcc@example.com>",),
        subject="Hello", body="Body",
    )
    raw, _ = build_draft_message("owner@example.com", draft)
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert [item.addr_spec for item in message["To"].addresses] == ["ada@example.com", "taro@example.jp"]
    assert message["To"].addresses[0].display_name == "Dr: Ada"
    assert message["To"].addresses[1].display_name == "山田太郎"
    assert message["Bcc"].addresses[0].display_name == "Private"


@pytest.mark.parametrize("value", ["a@example.com, b@example.com", "Team: a@example.com;", "<@old-route:a@example.com>", "a@example.com\r\nBcc: x@example.com"])
def test_recipient_entry_must_contain_exactly_one_safe_mailbox(value: str) -> None:
    with pytest.raises(ValueError, match="invalid To address|line breaks"):
        prepare_draft(to=(value,), subject="Hello", body="Body")


def test_reply_headers_are_emitted() -> None:
    draft = prepare_draft(to=("reader@example.com",), subject="Hello", body="Body", in_reply_to="<source@example.com>", references=("<root@example.com>", "<source@example.com>"))
    raw, _ = build_draft_message("owner@example.com", draft)
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert str(message["In-Reply-To"]) == "<source@example.com>"
    assert str(message["References"]) == "<root@example.com> <source@example.com>"
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


def test_authored_html_preserves_permissive_email_layout_css() -> None:
    html = (
        '<p style="margin:0;line-height:1.15">one</p>'
        '<div style="padding:1.5pt 2.25pt;background-color:#eee;text-align:right">two</div>'
        '<table style="width:100%;border:solid black 1.0pt"><tr>'
        '<td style="padding:1.5pt 2.25pt">cell</td></tr></table>'
    )
    prepared = prepare_html(html, maximum_bytes=100_000)
    for expected in (
        "margin:0", "line-height:1.15", "padding:1.5pt 2.25pt",
        "border:solid black 1.0pt", "background-color:#eee",
        "text-align:right", "width:100%",
    ):
        assert expected in prepared


def test_bordered_table_and_zero_margin_paragraphs_round_trip_through_read_policy() -> None:
    draft = prepare_draft(
        to=("to@example.com",), subject="layout", body="one\ntwo",
        html_body=(
            '<p style="margin:0;line-height:1.15">one</p>'
            '<p style="margin:0">two</p>'
            '<table style="border:solid black 1.0pt"><tr><td style="padding:2pt">x</td></tr></table>'
        ),
    )
    raw, _ = build_draft_message("owner@example.com", draft)
    message = BytesParser(policy=policy.default).parsebytes(raw)
    html_part = next(part for part in message.walk() if part.get_content_type() == "text/html")
    read_back = sanitize_inbound_html(html_part.get_content(), maximum_bytes=100_000)
    assert "margin:0" in read_back
    assert "line-height:1.15" in read_back
    assert "border:solid black 1.0pt" in read_back
    assert "padding:2pt" in read_back


@pytest.mark.parametrize(
    "style",
    [
        "background:url(http://x)", "background:image-set(url(http://x) 1x)",
        "display:none", "visibility:hidden", "opacity:0", "font-size:0px",
        "text-indent:-100px", "position:fixed", "position:absolute",
        "z-index:99", "transform:scale(2)", "content:'hidden'",
        "width:expression(1)", "behavior:url(x)", "color:javascript:red",
        "background:data:text/plain,x", r"color:r\65 d",
        "color:'red;blue'", "color:'<red>'",
    ],
)
def test_authored_html_rejects_remote_hidden_escape_and_parser_values(style: str) -> None:
    with pytest.raises(ValueError):
        prepare_html(f'<p style="{style}">x</p>', maximum_bytes=100_000)


def test_authored_stylesheet_rejects_import() -> None:
    with pytest.raises(ValueError):
        prepare_html("<style>@import 'https://x';</style><p>x</p>", maximum_bytes=100_000)


def test_inbound_policy_remains_strict_and_cannot_use_authored_policy() -> None:
    assert INBOUND_POLICY is not AUTHORED_POLICY
    authored = prepare_html('<p style="animation:pulse 1s;margin:0">x</p>', maximum_bytes=100_000)
    inbound = sanitize_inbound_html(
        '<p style="animation:pulse 1s;position:fixed;z-index:9;transform:scale(2);content:x">x</p>'
        '<img src="https://x"><link href="https://x" srcset="https://x">',
        maximum_bytes=100_000,
    )
    assert "animation:pulse 1s" in authored
    for unsafe in ("animation", "position", "z-index", "transform", "content", "https://x", "srcset"):
        assert unsafe not in inbound


def test_inbound_html_removes_every_remote_resource_surface() -> None:
    html = """
      <style>@import url(https://x); p { background:url(https://x); }</style>
      <p style="background-image:url(https://x);list-style-image:url(https://x);
        border-image:url(https://x);cursor:url(https://x);mask:url(https://x);
        background:image-set(url(https://x) 1x)">text</p>
      <img src="https://x" srcset="https://x 1x"><link href="https://x">
      <object data="https://x"></object><iframe src="https://x"></iframe>
      <video src="https://x"></video><audio src="https://x"></audio>
    """
    result = sanitize_inbound_html(html, maximum_bytes=100_000)
    for marker in ("https://x", "url(", "@import", "image-set", "src=", "srcset"):
        assert marker not in result


def test_empty_paragraphs_are_preserved_in_every_position_for_both_policies() -> None:
    html = "<p></p><p>&nbsp;</p><p>text</p><p></p><p>&nbsp;</p>"
    authored = prepare_html(html, maximum_bytes=100_000)
    inbound = sanitize_inbound_html(html, maximum_bytes=100_000)
    for result in (authored, inbound):
        assert result.count("<p></p>") == 2
        assert result.count("<p>&nbsp;</p>") == 2


def test_inbound_html_round_trips_into_authored_draft() -> None:
    inbound = sanitize_inbound_html(
        '<div><p>hi <a href="https://example.com/a">link</a></p></div>',
        maximum_bytes=2 * 1024 * 1024,
    )
    assert "rel=" in inbound
    prepare_html(inbound, maximum_bytes=2 * 1024 * 1024)


def test_authored_rel_value_is_replaced_by_the_server_value() -> None:
    out = prepare_html(
        '<p><a href="https://x.com/" rel="dns-prefetch">x</a></p>',
        maximum_bytes=2 * 1024 * 1024,
    )
    assert "dns-prefetch" not in out
    assert out.count('rel="noopener noreferrer"') == 1


def test_authored_error_names_the_offending_attribute() -> None:
    with pytest.raises(AuthoredHtmlError, match="ping"):
        prepare_html(
            '<p><a href="https://x.com/" ping="x">y</a></p>',
            maximum_bytes=2 * 1024 * 1024,
        )


def test_authored_error_detail_is_bounded_and_scrubbed() -> None:
    with pytest.raises(AuthoredHtmlError) as exc:
        prepare_html("<p><evil tag>x</evil tag></p>", maximum_bytes=2 * 1024 * 1024)
    assert len(str(exc.value)) <= 200


@pytest.mark.parametrize(
    "html",
    [
        '<p><a href="tel:+85231455524">call</a></p>',
        '<p style="font-family:Arial,\n sans-serif">x</p>',
        '<p dir="rtl" lang="he">sh</p>',
        '<p><font face="Arial" size="2" color="#333">x</font></p>',
        '<p><ruby>K<rt>k</rt></ruby></p>',
        '<p><del>a</del><ins>b</ins><mark>m</mark><abbr>A</abbr><q>q</q><cite>c</cite></p>',
        '<figure><figcaption>c</figcaption></figure>',
        '<p><bdi>a</bdi><bdo dir="rtl">b</bdo>c<wbr>d</p>',
        '<p><a href="https://x.com/" target="_self">x</a></p>',
        '<p><a name="top">x</a></p>',
        '<table><tr><td headers="h1" abbr="s">x</td></tr></table>',
    ],
)
def test_inert_constructs_are_accepted(html: str) -> None:
    prepare_html(html, maximum_bytes=2 * 1024 * 1024)


def test_dir_and_lang_survive_to_output() -> None:
    out = prepare_html('<p dir="rtl" lang="he">sh</p>', maximum_bytes=2 * 1024 * 1024)
    assert 'dir="rtl"' in out and 'lang="he"' in out


def test_bad_dir_value_is_dropped_not_raised() -> None:
    out = prepare_html('<p dir="../../etc">x</p>', maximum_bytes=2 * 1024 * 1024)
    assert "../../etc" not in out


def test_inbound_keeps_line_wrapped_styles() -> None:
    out = sanitize_inbound_html(
        '<p style="color:red;\n font-size:9pt">x</p>',
        maximum_bytes=2 * 1024 * 1024,
    )
    assert "color:red" in out


@pytest.mark.parametrize(
    "html",
    [
        '<p style="display:none">x</p>',
        '<p style="visibility:hidden">x</p>',
        '<p style="opacity:0.01">x</p>',
        '<p style="font-size:0">x</p>',
        '<p style="position:fixed">x</p>',
        '<p style="text-indent:-9999px">x</p>',
        '<p style="background:url(https://x/y.png)">x</p>',
        '<style>@import url(https://x/y.css);</style><p>x</p>',
        '<p style="background-image:data:image/png;base64,AA">x</p>',
        '<p><img src="https://x/y.png"></p>',
        '<p><script>alert(1)</script></p>',
        '<p><iframe src="https://x/"></iframe></p>',
        '<form><input name="a"></form>',
        '<p onclick="x()">y</p>',
        '<p><a href="javascript:alert(1)">x</a></p>',
        '<p><a href="data:text/html,x">x</a></p>',
        '<p><a href="https://x.com/" ping="https://e/">x</a></p>',
        '<p rel="noopener">x</p>',
        '<p style="color:red;expression(1)">x</p>',
        '<p style="background:url (https://x/y.png)">x</p>',
        '<p style="background:url\n(https://x/y.png)">x</p>',
    ],
)
def test_authored_html_threat_model_is_still_rejected(html: str) -> None:
    with pytest.raises(ValueError):
        prepare_html(html, maximum_bytes=2 * 1024 * 1024)


@pytest.mark.parametrize(
    "html",
    [
        '<p style="font-family:Arial,\n sans-serif">x</p>',
        '<p style="color:red;\n font-size:9pt">x</p>',
        '<p style="border:solid black\n 1.0pt">x</p>',
    ],
)
def test_line_wrapped_values_are_accepted(html: str) -> None:
    prepare_html(html, maximum_bytes=2 * 1024 * 1024)


@pytest.mark.parametrize(
    "css",
    [
        "p { background-color: red; background-image: url(https://tracker.example/x); }",
        "p { border: url(https://tracker.example/x); }",
        "@import url(https://tracker.example/x); p { color: red; }",
        "p { position: fixed; z-index: 999; }",
        "p { display: none; }",
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
