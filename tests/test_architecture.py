from __future__ import annotations

from pathlib import Path


def test_package_boundaries_exist() -> None:
    package = Path("src/readndraft_imap_mcp")
    expected = {
        "admin",
        "attachments",
        "audit",
        "broker",
        "credentials",
        "drafts",
        "imap",
        "ipc",
        "mcp_server",
        "mime",
        "platform",
    }
    assert expected <= {
        child.name
        for child in package.iterdir()
        if child.is_dir() and (child / "__init__.py").is_file()
    }
