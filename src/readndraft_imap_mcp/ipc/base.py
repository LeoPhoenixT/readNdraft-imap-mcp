from __future__ import annotations

from typing import Protocol

from readndraft_imap_mcp.broker.protocol import BrokerRequest, BrokerResponse


class BrokerTransport(Protocol):
    """Transport for typed broker requests; raw protocol commands are excluded."""

    async def request(self, request: BrokerRequest) -> BrokerResponse: ...

