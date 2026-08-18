from __future__ import annotations

from pathlib import Path


def test_readme_documents_plugin_setup_security_and_support() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
    for required in (
        "uvx readndraft-imap-mcp@0.5.0 setup",
        "codex plugin add readndraft@readndraft",
        "/plugin install readndraft@readndraft",
        "migrate-plugin --client codex",
        "cannot send, submit, or delete",
        "Batch-read plain text for up to 10 selected messages",
        "## Authorization boundary",
        "never authorization",
        "broker prefers",
        "not MCP tools",
        "ambiguous move outcome",
        "`multipart/alternative`",
        "HTML-only messages are converted",
        "complete HTML documents",
        "allowlisted CSS",
        "never fetched automatically",
    ):
        assert required in text
    assert "uvx readndraft-imap-mcp approve" not in text
    assert "[mcp_servers.readndraft]" not in text


def test_security_document_explains_inbound_and_outbound_html_policies() -> None:
    text = Path("docs/SECURITY.md").read_text(encoding="utf-8")
    for required in (
        "selected HTML body is converted to bounded plain text",
        "separate outbound composition policy",
        "safe links",
        "CSS allowlist",
        "normalized, and inlined",
        "all images cause the draft request to be rejected",
        "Processing never resolves",
        "fetches a URL",
        "equivalent required plain-text",
        "`body`",
    ):
        assert required in text


def test_readme_repository_markdown_links_exist_and_are_pypi_safe() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
    repo_prefix = "https://github.com/LeoPhoenixT/readNdraft-imap-mcp/blob/main/"
    repo_targets = []
    relative_targets = []
    for fragment in text.split("](")[1:]:
        target = fragment.split(")", 1)[0]
        if target.startswith(repo_prefix):
            repo_targets.append(target.removeprefix(repo_prefix))
        elif "://" not in target and not target.startswith("#"):
            relative_targets.append(target)
    assert repo_targets
    assert relative_targets == []
    assert all(Path(target).exists() for target in repo_targets)
