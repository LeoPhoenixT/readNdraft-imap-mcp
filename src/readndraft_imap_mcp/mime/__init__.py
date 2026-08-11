"""Shared safe MIME parsing and generation package."""

from .parser import MessageLimitError, sanitize_filename

__all__ = ["MessageLimitError", "sanitize_filename"]

