from __future__ import annotations

import os
import platform
from dataclasses import asdict, dataclass
from typing import Literal, TypeAlias

from readndraft_imap_mcp import __version__
from readndraft_imap_mcp.protocol_version import IPC_PROTOCOL_VERSION


class ProtocolError(ValueError):
    """Raised when an internal broker request is outside the typed allowlist."""


@dataclass(frozen=True, slots=True)
class HealthRequest:
    operation: Literal["health"] = "health"


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    task_bucket_capacity: int = 120
    task_refill_per_second: float = 2.0
    account_sessions: int = 2
    imap_workers: int = 8
    waiting_imap_work: int = 16


@dataclass(frozen=True, slots=True)
class ResourceRejections:
    task_rate: int = 0
    session_queue_timeout: int = 0
    imap_worker_capacity: int = 0


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    active_sessions: int = 0
    queued_session_requests: int = 0
    rejections: ResourceRejections = ResourceRejections()


@dataclass(frozen=True, slots=True)
class HealthResponse:
    ok: Literal[True] = True
    status: Literal["healthy"] = "healthy"
    protocol_version: int = IPC_PROTOCOL_VERSION
    package_version: str = __version__
    python_version: str = platform.python_version()
    python_implementation: str = platform.python_implementation()
    pid: int = os.getpid()
    resource_limits: ResourceLimits = ResourceLimits()
    resource_usage: ResourceUsage = ResourceUsage()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


BrokerRequest: TypeAlias = HealthRequest
BrokerResponse: TypeAlias = HealthResponse


def decode_request(payload: object) -> BrokerRequest:
    """Decode the compatibility health operation allowlist."""
    if not isinstance(payload, dict) or payload != {"operation": "health"}:
        raise ProtocolError("operation is not allowed")
    return HealthRequest()
