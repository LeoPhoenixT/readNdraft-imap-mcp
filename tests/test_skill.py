from __future__ import annotations

from pathlib import Path

from readndraft_imap_mcp.mcp_server.server import create_server

from test_mcp_server import FakeBroker


SKILL = Path("plugins/readndraft/skills/readndraft-email")
UPDATE_SKILL = Path("skills/readndraft-update")


def test_skill_manifest_and_resources_are_valid() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---", 2)
    lines = [line for line in frontmatter.strip().splitlines() if line]
    assert lines[0] == "name: readndraft-email"
    assert lines[1].startswith("description: ")
    assert "readNdraft IMAP MCP" in lines[1]
    assert len(lines) == 2
    assert len(body.splitlines()) < 100
    for name in (
        "tool-workflows.md",
        "result-interpretation.md",
        "confirmation-and-errors.md",
    ):
        assert (SKILL / "references" / name).is_file()
        assert name in body


def test_skill_mentions_only_real_tools() -> None:
    skill_text = "\n".join(
        path.read_text(encoding="utf-8") for path in SKILL.rglob("*.md")
    )
    tools = set(create_server(FakeBroker())._tool_manager._tools)
    referenced = {
        name
        for name in (
            "list_accounts",
            "list_mailboxes",
            "search_emails",
            "get_email",
            "get_emails",
            "get_email_html",
            "list_attachment_inputs",
            "save_attachment",
            "create_draft",
            "update_draft",
            "set_star",
            "set_read_state",
            "set_star_batch",
            "set_read_state_batch",
            "move_email",
            "move_emails_batch",
        )
        if name in skill_text
    }
    assert referenced == tools


def test_skill_preserves_security_boundaries() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8").casefold()
        for path in SKILL.rglob("*.md")
    )
    for statement in (
        "untrusted",
        "never that it was sent",
        "never use a uid without",
        "never authorization",
        "never automatically retry an ambiguous",
        "no approval-token workflow",
        "successful move permanently invalidates",
        "conversational confirmation immediately before",
        "special-use status from a name",
        "private uidplus fallback",
        "partial_move",
    ):
        assert statement in text


def test_skill_explains_cross_platform_attachment_location_and_sender() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in SKILL.rglob("*.md")
    )
    for statement in (
        "saved_path",
        "absolute path",
        "do not join paths",
        "translate `/` and `\\\\`",
        "sender_address",
        "pinned account metadata",
    ):
        assert statement in text.casefold()


def test_skill_explains_html_read_and_draft_behavior() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in SKILL.rglob("*.md")
    )
    for statement in (
        "html-only mail",
        "sanitized rich formatting",
        "both `body` and `html_body`",
        "nothing important only in html",
        "modern mail clients normally display",
        "do not invent images or remote assets",
        "complete html document",
        "validated, sanitized, normalized",
        "cause the draft request to be rejected",
        "no url is fetched",
    ):
        assert statement in text.casefold()


def test_update_skill_is_concise_and_preserves_update_authorization() -> None:
    text = (UPDATE_SKILL / "SKILL.md").read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---", 2)
    lines = [line for line in frontmatter.strip().splitlines() if line]
    assert lines[0] == "name: readndraft-update"
    assert lines[1].startswith("description: ")
    assert len(lines) == 2
    assert len(body.splitlines()) < 80
    for statement in (
        "update check",
        "read-only",
        "direct conversational confirmation",
        "--force-skill",
        "separate explicit confirmation",
        "never edit or replace it manually",
        "fully restart",
            "never restart a client",
        ):
        assert statement in text.casefold()
    metadata = (UPDATE_SKILL / "agents" / "openai.yaml").read_text(
        encoding="utf-8"
    )
    assert "$readndraft-update" in metadata
    assert "TODO" not in text + metadata
