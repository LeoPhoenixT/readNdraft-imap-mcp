"""Typed local broker transport interfaces with lazy, unprivileged imports."""

from .base import BrokerTransport

__all__ = ["ALLOWED_OPERATIONS", "BrokerRpcServer", "BrokerTransport", "IpcBrokerClient", "RpcError"]


def __getattr__(name: str):
    if name == "IpcBrokerClient":
        from .client import IpcBrokerClient

        return IpcBrokerClient
    if name == "BrokerRpcServer":
        from .server import BrokerRpcServer

        return BrokerRpcServer
    if name in {"ALLOWED_OPERATIONS", "RpcError"}:
        from .contract import ALLOWED_OPERATIONS, RpcError

        return {"ALLOWED_OPERATIONS": ALLOWED_OPERATIONS, "RpcError": RpcError}[name]
    raise AttributeError(name)
