from __future__ import annotations

from readndraft_imap_mcp.credentials import backend


def test_backend_info_contains_no_secret_fields() -> None:
    fields = backend.CredentialBackendInfo.__dataclass_fields__
    assert set(fields) == {"platform", "implementation", "approved"}


