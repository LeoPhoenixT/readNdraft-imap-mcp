from __future__ import annotations

from email import policy
from email.parser import BytesParser

import pytest

from readndraft_imap_mcp.mime.drafts import (
    DraftAttachment,
    build_draft_message,
    prepare_draft,
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
    assert message["Cc"] is None
    assert message["Bcc"] is None


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
