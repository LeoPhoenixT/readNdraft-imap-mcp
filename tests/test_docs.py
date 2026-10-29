from __future__ import annotations

from pathlib import Path


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
