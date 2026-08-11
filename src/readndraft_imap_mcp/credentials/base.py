from __future__ import annotations

from typing import Protocol


class CredentialStore(Protocol):
    """Platform credential interface; implementations belong in the broker."""

    async def save_secret(self, account_id: str, secret: str) -> None: ...

    async def load_secret(self, account_id: str) -> str: ...

    async def delete_secret(self, account_id: str) -> None: ...

