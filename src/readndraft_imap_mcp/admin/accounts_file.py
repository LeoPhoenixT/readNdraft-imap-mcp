from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, replace
from pathlib import Path

from readndraft_imap_mcp.broker.accounts import AccountConfig, AccountRegistry


class AccountFile:
    """Human-administered pinned account metadata; never stores secrets."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("account file path must be absolute")
        self.path = path

    def load(self) -> tuple[AccountConfig, ...]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        except json.JSONDecodeError as exc:
            raise ValueError("invalid account configuration JSON") from exc
        if not isinstance(value, list):
            raise ValueError("account configuration must be a list")
        try:
            accounts = tuple(AccountConfig(**item) for item in value if isinstance(item, dict))
        except TypeError as exc:
            raise ValueError("invalid account configuration") from exc
        if len(accounts) != len(value):
            raise ValueError("invalid account configuration entry")
        AccountRegistry(accounts)
        return accounts

    def registry(self) -> AccountRegistry:
        return AccountRegistry(self.load())

    def _write(self, accounts: tuple[AccountConfig, ...]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_suffix(f".{secrets.token_hex(4)}.tmp")
        temporary.write_text(
            json.dumps([asdict(account) for account in accounts], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def upsert(self, account: AccountConfig) -> None:
        values = {item.account_id: item for item in self.load()}
        values[account.account_id] = account
        self._write(tuple(values[key] for key in sorted(values)))

    def set_enabled(self, account_id: str, enabled: bool) -> None:
        values = {item.account_id: item for item in self.load()}
        if account_id not in values:
            raise KeyError("unknown account_id")
        values[account_id] = replace(values[account_id], enabled=enabled)
        self._write(tuple(values[key] for key in sorted(values)))

    def set_sender_address(self, account_id: str, sender_address: str | None) -> None:
        values = {item.account_id: item for item in self.load()}
        if account_id not in values:
            raise KeyError("unknown account_id")
        values[account_id] = replace(
            values[account_id], sender_address=sender_address
        )
        self._write(tuple(values[key] for key in sorted(values)))

    def delete(self, account_id: str) -> None:
        values = {item.account_id: item for item in self.load()}
        if account_id not in values:
            raise KeyError("unknown account_id")
        del values[account_id]
        self._write(tuple(values[key] for key in sorted(values)))
