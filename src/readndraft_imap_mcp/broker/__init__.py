"""Capability-minimized broker package."""

from .accounts import AccountConfig, AccountRegistry
from .limits import AccountRequestQuota, RequestQuotaError
from .protocol import HealthRequest, HealthResponse, ProtocolError, decode_request

__all__ = [
    "BrokerService",
    "AccountConfig",
    "AccountRequestQuota",
    "AccountRegistry",
    "HealthRequest",
    "HealthResponse",
    "ProtocolError",
    "RequestQuotaError",
    "decode_request",
]


def __getattr__(name: str):
    if name == "BrokerService":
        from .service import BrokerService
        return BrokerService
    raise AttributeError(name)
