from __future__ import annotations

import os
import threading

import pytest

from readndraft_imap_mcp.platform.install import linux_unit, windows_task_command
from readndraft_imap_mcp.platform.paths import AppPaths


def _paths(tmp_path) -> AppPaths:
    return AppPaths(
        (tmp_path / "config").resolve(),
        (tmp_path / "state").resolve(),
        (tmp_path / "runtime").resolve(),
    )


def test_ipc_key_is_stable_and_private(tmp_path) -> None:
    paths = _paths(tmp_path)
    first = paths.load_or_create_ipc_key()
    second = paths.load_or_create_ipc_key()

    assert len(first) == 32
    assert first == second
    if os.name != "nt":
        assert paths.ipc_key_file.stat().st_mode & 0o777 == 0o600
        assert paths.runtime_dir.stat().st_mode & 0o777 == 0o700


def test_ipc_key_creation_is_atomic_across_simultaneous_launchers(tmp_path) -> None:
    paths = _paths(tmp_path)
    barrier = threading.Barrier(8)
    keys: list[bytes] = []
    errors: list[BaseException] = []

    def load():
        try:
            barrier.wait()
            keys.append(paths.load_or_create_ipc_key())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=load) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert len(keys) == 8
    assert len(set(keys)) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows named-pipe isolation")
def test_windows_ipc_address_is_isolated_by_runtime_directory(tmp_path) -> None:
    first = _paths(tmp_path / "first")
    second = _paths(tmp_path / "second")

    assert first.ipc_address.startswith(r"\\.\pipe\readndraft-broker-v")
    assert first.ipc_address == _paths(tmp_path / "first").ipc_address
    assert first.ipc_address != second.ipc_address


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission check")
def test_ipc_key_rejects_weak_permissions(tmp_path) -> None:
    paths = _paths(tmp_path)
    paths.load_or_create_ipc_key()
    os.chmod(paths.ipc_key_file, 0o644)

    with pytest.raises(PermissionError, match="group/world"):
        paths.load_or_create_ipc_key()


def test_service_templates_use_unprivileged_user_context() -> None:
    unit = linux_unit()
    task = windows_task_command()

    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "WantedBy=default.target" in unit
    assert "/RL LIMITED" in task
    assert "/SC ONLOGON" in task
