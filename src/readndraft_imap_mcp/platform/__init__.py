"""Cross-platform adapter selection interfaces."""

from .base import PlatformAdapter
from .paths import AppPaths, current_app_paths

__all__ = ["AppPaths", "PlatformAdapter", "current_app_paths"]
