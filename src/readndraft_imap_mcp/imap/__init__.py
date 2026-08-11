"""Shared IMAP protocol implementation package."""

from .client import ImapClient
from .models import DraftUpdateResult, MessageIdentity, SearchFilters

__all__ = ["DraftUpdateResult", "ImapClient", "MessageIdentity", "SearchFilters"]
