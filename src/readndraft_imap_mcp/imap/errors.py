"""Shared IMAP operation failures without client runtime dependencies."""

from __future__ import annotations


class ImapClientError(RuntimeError):
    """Raised when a production read-only IMAP operation fails closed."""


class ImapMovePartialError(ImapClientError):
    """Raised when a fallback copy succeeded but move completion is uncertain."""
