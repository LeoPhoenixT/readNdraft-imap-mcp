"""Cross-platform adapter selection interfaces."""

from .paths import AppPaths, current_app_paths

__all__ = ["AppPaths", "PlatformAdapter", "current_app_paths"]


def __getattr__(name: str):
    if name == "PlatformAdapter":
        from .base import PlatformAdapter
        return PlatformAdapter
    raise AttributeError(name)
