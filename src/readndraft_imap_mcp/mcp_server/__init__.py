"""Capability-minimized stdio MCP frontend package."""

__all__ = ["create_server"]


def __getattr__(name: str):
    if name == "create_server":
        from .server import create_server

        return create_server
    raise AttributeError(name)

