from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from readndraft_imap_mcp import __version__
from readndraft_imap_mcp.protocol_version import IPC_PROTOCOL_VERSION

MINIMUM_PYTHON = (3, 12, 6)
MINIMUM_PYTHON_TEXT = ">=3.12.6"


@dataclass(frozen=True, slots=True)
class BrokerCompatibility:
    compatible: bool
    reason: str


def _python_tuple(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[a-zA-Z0-9.+-]*)?", value)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def broker_compatibility(health: Mapping[str, object] | None) -> BrokerCompatibility:
    """Apply the package's single broker/frontend compatibility policy."""
    if health is None:
        return BrokerCompatibility(False, "broker is not running")
    if health.get("ok") is not True or health.get("status") != "healthy":
        return BrokerCompatibility(False, "broker health status is not healthy")
    if health.get("protocol_version") != IPC_PROTOCOL_VERSION:
        return BrokerCompatibility(False, "IPC protocol version differs")
    if health.get("package_version") != __version__:
        return BrokerCompatibility(False, "readNdraft package version differs")
    runtime = _python_tuple(health.get("python_version"))
    if runtime is None:
        return BrokerCompatibility(False, "broker Python runtime is not reported")
    if runtime < MINIMUM_PYTHON:
        return BrokerCompatibility(False, f"requires Python {MINIMUM_PYTHON_TEXT}")
    if not isinstance(health.get("python_implementation"), str):
        return BrokerCompatibility(False, "broker Python implementation is not reported")
    if not isinstance(health.get("pid"), int) or isinstance(health.get("pid"), bool):
        return BrokerCompatibility(False, "broker PID is not reported")
    return BrokerCompatibility(True, "compatible")
