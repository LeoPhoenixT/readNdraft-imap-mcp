from __future__ import annotations

import asyncio
import inspect

import pytest

from readndraft_imap_mcp.imap.models import SearchFilters
from readndraft_imap_mcp.mcp_server.backend import BrokerBackend, UnavailableBroker


def test_unavailable_backend_accepts_every_protocol_call_shape() -> None:
    backend = UnavailableBroker()
    for name, method in vars(BrokerBackend).items():
        if name.startswith("_") or not callable(method):
            continue
        parameters = list(inspect.signature(method).parameters.values())[1:]
        positional = [object() for parameter in parameters if parameter.kind == parameter.POSITIONAL_OR_KEYWORD]
        keyword_only = {
            parameter.name: object() for parameter in parameters if parameter.kind == parameter.KEYWORD_ONLY
        }
        implementation = inspect.signature(getattr(backend, name))
        implementation.bind(*positional, **keyword_only)
        implementation.bind(**{parameter.name: object() for parameter in parameters})


@pytest.mark.parametrize("cursor", [None, "opaque-cursor"])
def test_unavailable_search_with_cursor_fails_closed(cursor) -> None:
    with pytest.raises(RuntimeError, match="broker connection is not configured"):
        asyncio.run(UnavailableBroker().search_email_targets((("synthetic", "INBOX"),), SearchFilters(), cursor=cursor))
