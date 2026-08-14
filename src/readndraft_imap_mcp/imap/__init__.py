"""Shared IMAP protocol implementation package."""

from .models import DraftUpdateResult, MessageIdentity, SearchFilters

__all__ = ["DraftUpdateResult", "ImapClient", "MessageIdentity", "SearchFilters"]


def __getattr__(name: str):
    # Import the client lazily so MIME parsing can import the model types without
    # creating an imap.client -> mime.parser -> imap package cycle.
    if name == "ImapClient":
        from .client import ImapClient

        return ImapClient
    raise AttributeError(name)
