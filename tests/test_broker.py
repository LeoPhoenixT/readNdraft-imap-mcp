from __future__ import annotations

import pytest

from readndraft_imap_mcp.protocol_version import IPC_PROTOCOL_VERSION

from readndraft_imap_mcp.broker import (
    BrokerService,
    HealthRequest,
    ProtocolError,
    decode_request,
)


def test_health_is_the_only_phase1_broker_operation() -> None:
    assert decode_request({"operation": "health"}) == HealthRequest()
    assert BrokerService().handle({"operation": "health"}) == {
        "ok": True,
        "status": "healthy",
        "protocol_version": IPC_PROTOCOL_VERSION,
    }


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "health",
        {},
        {"operation": "health", "extra": True},
        {"operation": "raw_imap", "command": "NOOP"},
    ],
)
def test_broker_rejects_every_non_health_request(payload: object) -> None:
    with pytest.raises(ProtocolError, match="not allowed"):
        BrokerService().handle(payload)
