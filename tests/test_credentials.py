from __future__ import annotations

from readndraft_imap_mcp.credentials import backend
from readndraft_imap_mcp.poc import credentials


def test_poc_credentials_reexports_backend_symbols() -> None:
    """Keep the supported readndraft-poc credential import path compatible."""
    assert credentials.CredentialBackendError is backend.CredentialBackendError
    assert credentials.CredentialBackendInfo is backend.CredentialBackendInfo
    assert credentials.delete_secret is backend.delete_secret
    assert credentials.inspect_backend is backend.inspect_backend
    assert credentials.load_secret is backend.load_secret
    assert credentials.require_approved_backend is backend.require_approved_backend
    assert credentials.save_secret is backend.save_secret


def test_backend_info_contains_no_secret_fields() -> None:
    fields = credentials.CredentialBackendInfo.__dataclass_fields__
    assert set(fields) == {"platform", "implementation", "approved"}


