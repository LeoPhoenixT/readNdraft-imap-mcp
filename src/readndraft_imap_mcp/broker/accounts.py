from __future__ import annotations

import re
from dataclasses import dataclass
from email.errors import HeaderParseError
from email.headerregistry import Address
from types import MappingProxyType
from typing import Iterable, Literal, Mapping

_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class AccountConfig:
    account_id: str
    hostname: str
    port: int
    username: str
    auth_method: Literal["login", "plain"] = "login"
    tls_mode: Literal["implicit"] = "implicit"
    enabled: bool = True
    sender_address: str | None = None

    def __post_init__(self) -> None:
        if not _ACCOUNT_ID_RE.fullmatch(self.account_id):
            raise ValueError("invalid account_id")
        if not self.hostname or any(char.isspace() for char in self.hostname):
            raise ValueError("invalid IMAP hostname")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if not self.username.strip():
            raise ValueError("username must be non-empty")
        if self.sender_address is not None:
            try:
                parsed_sender = Address(addr_spec=self.sender_address)
            except (HeaderParseError, ValueError):
                raise ValueError("invalid sender_address")
            if (
                parsed_sender.addr_spec != self.sender_address
                or not parsed_sender.username
                or not parsed_sender.domain
            ):
                raise ValueError("invalid sender_address")

    @property
    def effective_sender_address(self) -> str:
        return self.sender_address or self.username

    def safe_metadata(self) -> dict[str, str | int | bool]:
        local, separator, domain = self.username.partition("@")
        masked = f"{local[:1]}***@{domain}" if separator else "***"
        return {
            "id": self.account_id,
            "username": masked,
            "host": self.hostname,
            "port": self.port,
            "enabled": self.enabled,
            "sender_address": self.effective_sender_address,
        }


class AccountRegistry:
    """Immutable administrative account configuration visible to the broker."""

    def __init__(self, accounts: Iterable[AccountConfig]) -> None:
        values = list(accounts)
        indexed = {account.account_id: account for account in values}
        if len(indexed) != len(values):
            raise ValueError("account_id values must be unique")
        self._accounts: Mapping[str, AccountConfig] = MappingProxyType(indexed)

    def list_safe(self) -> list[dict[str, str | int | bool]]:
        return [
            self._accounts[key].safe_metadata()
            for key in sorted(self._accounts)
        ]

    def require_enabled(self, account_id: str) -> AccountConfig:
        try:
            account = self._accounts[account_id]
        except KeyError as exc:
            raise KeyError("unknown account_id") from exc
        if not account.enabled:
            raise PermissionError("account is disabled")
        return account
