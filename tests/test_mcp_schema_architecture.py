from __future__ import annotations

import subprocess
import sys


def test_schema_import_does_not_load_frontend_or_privileged_runtime():
    code = """
import sys
import readndraft_imap_mcp.mcp_server.schemas
blocked = (
    'readndraft_imap_mcp.mcp_server.server',
    'readndraft_imap_mcp.ipc',
    'readndraft_imap_mcp.broker',
    'readndraft_imap_mcp.credentials',
    'readndraft_imap_mcp.admin',
)
assert not [name for name in sys.modules if name.startswith(blocked)]
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_package_create_server_compatibility_export_is_preserved():
    from readndraft_imap_mcp.mcp_server import create_server
    from readndraft_imap_mcp.mcp_server.server import create_server as ServerFactory

    assert create_server is ServerFactory
