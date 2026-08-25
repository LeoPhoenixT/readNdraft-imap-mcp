from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from readndraft_imap_mcp import __version__
from readndraft_imap_mcp.platform.broker_compatibility import broker_compatibility
from readndraft_imap_mcp.platform.doctor import _broker_diagnostic
from readndraft_imap_mcp.protocol_version import IPC_PROTOCOL_VERSION


def _health(**changes: object) -> dict[str, object]:
    health: dict[str, object] = {
        "ok": True,
        "status": "healthy",
        "protocol_version": IPC_PROTOCOL_VERSION,
        "package_version": __version__,
        "python_version": "3.12.6",
        "python_implementation": "CPython",
        "pid": 123,
    }
    health.update(changes)
    return health


def test_package_and_lock_require_python_3126() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    lock = tomllib.loads(Path("uv.lock").read_text())
    assert project["requires-python"] == ">=3.12.6"
    assert lock["requires-python"] == project["requires-python"]


@pytest.mark.parametrize("runtime", ["3.12.6", "3.12.8", "3.13.0"])
def test_supported_same_package_runtime_is_compatible(runtime: str) -> None:
    assert broker_compatibility(_health(python_version=runtime)).compatible


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"python_version": "3.12.0"}, "requires Python >=3.12.6"),
        ({"package_version": "0.8.0"}, "package version differs"),
        ({"protocol_version": -1}, "protocol version differs"),
        ({"status": "starting"}, "not healthy"),
    ],
)
def test_incompatible_broker_reason(changes: dict[str, object], reason: str) -> None:
    result = broker_compatibility(_health(**changes))
    assert not result.compatible
    assert reason in result.reason


def test_legacy_health_without_runtime_identity_is_incompatible() -> None:
    health = _health()
    health.pop("python_version")
    health.pop("python_implementation")
    health.pop("pid")
    assert not broker_compatibility(health).compatible


def test_implementation_is_diagnostic_not_a_restriction() -> None:
    assert broker_compatibility(_health(python_implementation="PyPy")).compatible


def test_doctor_reports_frontend_broker_runtime_compatibility() -> None:
    compatible, detail = _broker_diagnostic(_health(python_version="3.12.0"))
    assert not compatible
    assert "PID 123" in detail
    assert f"readNdraft {__version__}" in detail
    assert "Python 3.12.0 (CPython)" in detail
    assert "compatible NO" in detail
    assert "requires Python >=3.12.6" in detail
