from __future__ import annotations

import subprocess
import sys
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
        child.name for child in package.iterdir() if child.is_dir() and (child / "__init__.py").is_file()
    }


def test_frontend_import_does_not_load_privileged_broker_or_credentials() -> None:
    code = """
import sys
import readndraft_imap_mcp.mcp_server.server
blocked = [name for name in sys.modules if name.startswith((
    'readndraft_imap_mcp.broker.service',
    'readndraft_imap_mcp.credentials',
    'readndraft_imap_mcp.admin',
))]
assert not blocked, blocked
"""
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr or completed.stdout
