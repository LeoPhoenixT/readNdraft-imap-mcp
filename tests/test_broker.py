from __future__ import annotations

import os
import platform

import pytest

from readndraft_imap_mcp import __version__
from readndraft_imap_mcp.broker import (
    BrokerService,
    HealthRequest,
    ProtocolError,
    decode_request,
)
from readndraft_imap_mcp.broker.service import _mutation_spec, _reply_thread
from readndraft_imap_mcp.protocol_version import IPC_PROTOCOL_VERSION


def test_reply_thread_normalizes_references_and_rejects_invalid_source() -> None:
    assert _reply_thread("<source@example.com>", "<root@example.com> <source@example.com>") == (
        "<source@example.com>",
        ("<root@example.com>", "<source@example.com>"),
    )
    with pytest.raises(ValueError, match="source Message-ID"):
        _reply_thread("not-a-message-id", None)


def test_mutation_spec_rejects_unknown_operations() -> None:
    with pytest.raises(ValueError, match="unsupported mutation operation"):
        _mutation_spec("unknown")


def test_health_contract_is_restricted_to_health() -> None:
    assert decode_request({"operation": "health"}) == HealthRequest()
    health = BrokerService().handle({"operation": "health"})
    assert health == {
        "ok": True,
        "status": "healthy",
        "protocol_version": IPC_PROTOCOL_VERSION,
        "package_version": __version__,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "pid": os.getpid(),
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
