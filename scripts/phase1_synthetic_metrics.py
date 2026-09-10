"""Private-data-free baseline for broker request scheduling.

Run this script with either the current checkout or an exported git revision on
PYTHONPATH.  It deliberately uses no account configuration, message headers,
or message bodies; only counters and timing are emitted.
"""

from __future__ import annotations

import asyncio
import json
import threading
from time import perf_counter

from readndraft_imap_mcp.broker import AccountConfig, AccountRegistry, BrokerService
from readndraft_imap_mcp.imap.models import Mailbox


class Credentials:
    async def load_secret(self, account_id: str) -> str:
        return "synthetic"


class Client:
    connections = 0
    commands = 0
    transferred_bytes = 0

    def __init__(self, account, secret) -> None:
        type(self).connections += 1

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def list_mailboxes(self):
        type(self).commands += 1
        type(self).transferred_bytes += 32
        return (Mailbox("Synthetic", "/", ()),)


async def run() -> dict[str, int | float]:
    accounts = AccountRegistry(
        [AccountConfig("a", "synthetic.invalid", 993, "a@example.invalid"),
         AccountConfig("b", "synthetic.invalid", 993, "b@example.invalid")]
    )
    broker = BrokerService(accounts, Credentials(), Client)
    started = perf_counter()
    await broker.list_mailboxes_batch(("a", "b"))
    return {
        "connections": Client.connections,
        "imap_commands": Client.commands,
        "transferred_bytes": Client.transferred_bytes,
        "elapsed_ms": round((perf_counter() - started) * 1000, 3),
        "threads": threading.active_count(),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), sort_keys=True))
