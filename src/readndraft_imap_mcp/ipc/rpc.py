"""Compatibility exports for callers of the former monolithic IPC module."""

from .client import IpcBrokerClient, _json_kwargs
from .codec import (
    BROKER_REQUEST_TIMEOUT_SECONDS,
    BROKER_WATCHDOG_TIMEOUT_SECONDS,
    MAX_FRAME_BYTES,
    RPC_RESPONSE_TIMEOUT_SECONDS,
    _decode_request,
    _encode,
)
from .contract import _PARAMETERS, ALLOWED_OPERATIONS, RpcError
from .server import BrokerRpcServer, _safe_error

__all__ = [
    "ALLOWED_OPERATIONS",
    "BROKER_REQUEST_TIMEOUT_SECONDS",
    "BROKER_WATCHDOG_TIMEOUT_SECONDS",
    "BrokerRpcServer",
    "IpcBrokerClient",
    "MAX_FRAME_BYTES",
    "RPC_RESPONSE_TIMEOUT_SECONDS",
    "RpcError",
    "_PARAMETERS",
    "_decode_request",
    "_encode",
    "_json_kwargs",
    "_safe_error",
]
