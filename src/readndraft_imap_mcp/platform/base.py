from __future__ import annotations

from typing import Protocol

from readndraft_imap_mcp.credentials import CredentialStore
from readndraft_imap_mcp.ipc import BrokerTransport


class PlatformAdapter(Protocol):
    """Creates the OS-specific adapters used by the shared broker core."""

    @property
    def platform_name(self) -> str: ...

    def credential_store(self) -> CredentialStore: ...

    def broker_transport(self) -> BrokerTransport: ...

