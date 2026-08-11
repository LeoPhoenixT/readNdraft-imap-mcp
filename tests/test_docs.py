from __future__ import annotations

from pathlib import Path


def test_readme_documents_uvx_setup_security_and_support() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
    for required in (
        "uvx readndraft-imap-mcp@latest setup",
        "uvx readndraft-imap-mcp doctor --online",
        "~/.agents/skills",
        "cannot send, submit, delete, or move mail",
        "Batch-read plain text for up to 10 selected messages",
        "## Authorization boundary",
        "never authorization",
    ):
        assert required in text
    assert "uvx readndraft-imap-mcp approve" not in text


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
