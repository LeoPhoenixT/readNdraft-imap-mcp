from __future__ import annotations

import sys
from pathlib import Path

from readndraft_imap_mcp.broker import daemon
from readndraft_imap_mcp.platform.launcher import StartupLock
from readndraft_imap_mcp.platform.paths import AppPaths


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        (tmp_path / "config").resolve(),
        (tmp_path / "state").resolve(),
        (tmp_path / "runtime").resolve(),
    )


def test_broker_instance_lock_is_exclusive(tmp_path: Path) -> None:
    path = (tmp_path / "instance.lock").resolve()
    with StartupLock(path) as first, StartupLock(path) as second:
        assert first.try_acquire() is True
        assert second.try_acquire() is False
    with StartupLock(path) as third:
        assert third.try_acquire() is True


def test_second_daemon_exits_without_binding(monkeypatch, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure_private()
    monkeypatch.setattr(daemon, "current_app_paths", lambda: paths)
    with StartupLock(paths.broker_instance_lock_file) as lock:
        assert lock.try_acquire() is True
        assert daemon.main([]) == 1


def test_direct_console_stop_is_parsed_from_process_argv(monkeypatch) -> None:
    stops = []
    monkeypatch.setattr(sys, "argv", ["readndraft-broker", "stop"])
    monkeypatch.setattr(daemon, "_stop_broker", lambda: stops.append(True) or 0)
    assert daemon.main() == 0
    assert stops == [True]
