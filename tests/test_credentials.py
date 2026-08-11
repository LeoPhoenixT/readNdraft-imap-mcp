from __future__ import annotations

from readndraft_imap_mcp.poc.credentials import CredentialBackendInfo


def test_backend_info_contains_no_secret_fields() -> None:
    fields = CredentialBackendInfo.__dataclass_fields__
    assert set(fields) == {"platform", "implementation", "approved"}


