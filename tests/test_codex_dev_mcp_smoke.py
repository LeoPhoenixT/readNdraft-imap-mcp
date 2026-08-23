from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "codex_dev_mcp_smoke.py"
SKILL = ROOT / ".agents" / "skills" / "readndraft-local-mcp-test"
SPEC = importlib.util.spec_from_file_location("codex_dev_mcp_smoke", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_repo_local_codex_config_launches_checkout_with_uv() -> None:
    parsed = tomllib.loads((ROOT / ".codex" / "config.toml").read_text("utf-8"))
    server = parsed["mcp_servers"]["readndraft_dev"]

    assert server["command"] == "uv"
    assert server["args"] == [
        "run",
        "--locked",
        "--no-sync",
        "readndraft-imap-mcp",
        "mcp",
    ]
    assert server["cwd"] == "."
    assert server["required"] is False
    assert server["default_tools_approval_mode"] == "approve"
    assert "env" not in server


def test_smoke_parser_extracts_jsonl_and_final_message() -> None:
    events = smoke._parse_events(
        '{"type":"item.completed","item":{"type":"mcp_tool_call"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message",'
        f'"text":"{smoke.SUCCESS_MARKER}"}}}}\n'
    )
    items = list(smoke._event_items(events))

    assert smoke._final_message(items) == smoke.SUCCESS_MARKER


def test_expected_tools_are_metadata_free_to_discover() -> None:
    assert "list_accounts" in smoke.EXPECTED_TOOLS
    assert "create_draft" in smoke.EXPECTED_TOOLS
    assert "move_email" in smoke.EXPECTED_TOOLS
    assert "move_emails_batch" in smoke.EXPECTED_TOOLS
    assert all(isinstance(name, str) for name in smoke.EXPECTED_TOOLS)


def test_fresh_session_override_contains_complete_required_transport(tmp_path) -> None:
    values = smoke._required_mcp_overrides(tmp_path.resolve())
    rendered = "\n".join(values)

    for key in (
        "command",
        "args",
        "cwd",
        "startup_timeout_sec",
        "tool_timeout_sec",
        "required=true",
        "default_tools_approval_mode",
    ):
        assert key in rendered


def test_repository_skill_documents_safe_local_mcp_workflow() -> None:
    skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    _, frontmatter, body = skill_text.split("---", 2)
    metadata = [line for line in frontmatter.strip().splitlines() if line]

    assert metadata[0] == "name: readndraft-local-mcp-test"
    assert metadata[1].startswith("description: ")
    assert "TODO" not in skill_text
    for statement in (
        "uv run --locked pytest tests/test_codex_dev_mcp_smoke.py",
        "codex -C . mcp get readndraft_dev --json",
        "uv run --locked python scripts/codex_dev_mcp_smoke.py",
        "Do not add this live",
        "readndraft-email",
    ):
        assert statement in body

    agent_metadata = (SKILL / "agents" / "openai.yaml").read_text(
        encoding="utf-8"
    )
    assert "$readndraft-local-mcp-test" in agent_metadata
