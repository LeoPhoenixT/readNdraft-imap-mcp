"""Compatibility imports for the retired Phase 0 credential module."""

from __future__ import annotations

import secrets
from dataclasses import asdict

from readndraft_imap_mcp.credentials.backend import (
    CredentialBackendError,
    CredentialBackendInfo,
    delete_secret,
    inspect_backend,
    load_secret,
    require_approved_backend,
    save_secret,
)

__all__ = [
    "CredentialBackendError",
    "CredentialBackendInfo",
    "delete_secret",
    "inspect_backend",
    "load_secret",
    "require_approved_backend",
    "run_round_trip_probe",
    "save_secret",
]


def run_round_trip_probe() -> dict[str, str | bool]:
    """Store, compare, and delete a random diagnostic canary."""
    info = require_approved_backend()
    account_id = f"phase0-canary-{secrets.token_hex(8)}"
    canary = secrets.token_urlsafe(32)
    try:
        save_secret(account_id, canary)
        loaded = load_secret(account_id)
        matched = secrets.compare_digest(loaded or "", canary)
    finally:
        delete_secret(account_id)
    return {**asdict(info), "round_trip": matched, "canary_deleted": True}


