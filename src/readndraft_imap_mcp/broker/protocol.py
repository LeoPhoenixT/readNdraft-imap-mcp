from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, TypeAlias

from readndraft_imap_mcp.protocol_version import IPC_PROTOCOL_VERSION


class ProtocolError(ValueError):
    """Raised when an internal broker request is outside the typed allowlist."""


@dataclass(frozen=True, slots=True)
class HealthRequest:
    operation: Literal["health"] = "health"


@dataclass(frozen=True, slots=True)
class HealthResponse:
    ok: Literal[True] = True
    status: Literal["healthy"] = "healthy"
    protocol_version: int = IPC_PROTOCOL_VERSION

    def to_dict(self) -> dict[str, str | bool | int]:
        return asdict(self)


BrokerRequest: TypeAlias = HealthRequest
BrokerResponse: TypeAlias = HealthResponse


def decode_request(payload: object) -> BrokerRequest:
    """Decode only the operations implemented by the current broker phase."""
    if not isinstance(payload, dict) or payload != {"operation": "health"}:
        raise ProtocolError("operation is not allowed")
    return HealthRequest()
