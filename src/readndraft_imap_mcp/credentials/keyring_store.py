from __future__ import annotations

import asyncio

from readndraft_imap_mcp.credentials.backend import (
    delete_secret,
    load_secret,
    save_secret,
)


class KeyringCredentialStore:
    """Broker-side adapter for the approved OS keyring backend."""

    async def save_secret(self, account_id: str, secret: str) -> None:
        await asyncio.to_thread(save_secret, account_id, secret)

    async def load_secret(self, account_id: str) -> str:
        return await asyncio.to_thread(self.load_secret_sync, account_id)

    def load_secret_sync(self, account_id: str) -> str:
        """Run directly inside the broker-owned bounded credential executor."""
        secret = load_secret(account_id)
        if secret is None:
            raise KeyError("no credential stored for account_id")
        return secret

    async def delete_secret(self, account_id: str) -> None:
        await asyncio.to_thread(delete_secret, account_id)

