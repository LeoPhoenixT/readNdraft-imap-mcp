"""Approved OS credential backend operations."""

from __future__ import annotations

import sys
from dataclasses import dataclass

import keyring
from keyring.errors import KeyringError

SERVICE_NAME = "readndraft-imap-mcp"


class CredentialBackendError(RuntimeError):
    """Raised when the selected keyring is not an approved backend."""


@dataclass(frozen=True)
class CredentialBackendInfo:
    platform: str
    implementation: str
    approved: bool


def inspect_backend() -> CredentialBackendInfo:
    backend = keyring.get_keyring()
    implementation = f"{type(backend).__module__}.{type(backend).__name__}"
    lowered = implementation.lower()

    if sys.platform == "win32":
        approved = "keyring.backends.windows" in lowered
    elif sys.platform.startswith("linux"):
        approved = "keyring.backends.secretservice" in lowered
    else:
        approved = False

    return CredentialBackendInfo(
        platform=sys.platform,
        implementation=implementation,
        approved=approved,
    )


def require_approved_backend() -> CredentialBackendInfo:
    info = inspect_backend()
    if not info.approved:
        raise CredentialBackendError(
            "No approved credential backend is active. Windows requires the "
            "Windows keyring backend; Linux requires Secret Service. readNdraft "
            "will not fall back to plaintext, environment variables, or files. "
            f"Detected: {info.implementation}"
        )
    return info


def save_secret(account_id: str, secret: str) -> None:
    require_approved_backend()
    if not account_id.strip() or not secret:
        raise ValueError("account_id and secret must be non-empty")
    try:
        keyring.set_password(SERVICE_NAME, account_id, secret)
    except KeyringError as exc:
        raise CredentialBackendError("Credential storage failed") from exc


def load_secret(account_id: str) -> str | None:
    require_approved_backend()
    try:
        return keyring.get_password(SERVICE_NAME, account_id)
    except KeyringError as exc:
        raise CredentialBackendError("Credential retrieval failed") from exc


def delete_secret(account_id: str) -> None:
    require_approved_backend()
    try:
        keyring.delete_password(SERVICE_NAME, account_id)
    except keyring.errors.PasswordDeleteError:
        return
    except KeyringError as exc:
        raise CredentialBackendError("Credential deletion failed") from exc
