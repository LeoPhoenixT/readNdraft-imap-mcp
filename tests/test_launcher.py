from __future__ import annotations

import os
import threading
import time

import pytest

from readndraft_imap_mcp.platform.launcher import (
    StartupLock,
    ensure_broker,
    start_owned_broker,
)
from readndraft_imap_mcp.platform.paths import AppPaths


def _paths(tmp_path) -> AppPaths:
    return AppPaths(
        (tmp_path / "config").resolve(),
        (tmp_path / "state").resolve(),
        (tmp_path / "runtime").resolve(),
    )


def test_startup_lock_is_private_exclusive_and_crash_recoverable(tmp_path) -> None:
    paths = _paths(tmp_path)
    paths.ensure_private()
    first = StartupLock(paths.startup_lock_file)
    assert first.try_acquire() is True
    second = StartupLock(paths.startup_lock_file)
    try:
        assert second.try_acquire() is False
        first.close()
        assert second.try_acquire() is True
        if os.name != "nt":
            assert paths.startup_lock_file.stat().st_mode & 0o777 == 0o600
    finally:
        first.close()
        second.close()


def test_existing_healthy_broker_is_not_started_or_owned(tmp_path) -> None:
    starts = []

    assert ensure_broker(
        _paths(tmp_path),
        health_check=lambda paths, timeout: True,
        starter=lambda idle, grace: starts.append((idle, grace)),
    ) is False
    assert starts == []


def test_unavailable_broker_is_started_then_authenticated(tmp_path) -> None:
    running = False
    starts = []

    def health(paths, timeout):
        return running

    def start(idle, grace):
        nonlocal running
        starts.append((idle, grace))
        running = True

    assert ensure_broker(
        _paths(tmp_path), health_check=health, starter=start
    ) is True
    assert starts == [(300.0, 10.0)]


def test_simultaneous_launchers_start_exactly_one_broker(tmp_path) -> None:
    paths = _paths(tmp_path)
    running = False
    starts = 0
    state_lock = threading.Lock()
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def health(app_paths, timeout):
        with state_lock:
            return running

    def start(idle, grace):
        nonlocal running, starts
        with state_lock:
            starts += 1
            running = True

    def launch():
        barrier.wait()
        results.append(
            ensure_broker(paths, health_check=health, starter=start)
        )

    threads = [threading.Thread(target=launch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert starts == 1
    assert sorted(results) == [False, True]


def test_startup_failure_is_bounded_and_actionable(tmp_path) -> None:
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="account test <account_id>"):
        ensure_broker(
            _paths(tmp_path),
            startup_timeout=0.15,
            health_check=lambda paths, timeout: False,
            starter=lambda idle, grace: object(),
        )
    assert time.monotonic() - started < 1


@pytest.mark.skipif(os.name != "nt", reason="Windows process flags")
def test_owned_broker_uses_no_window_creation_flag(monkeypatch) -> None:
    captured = {}

    class Process:
        def wait(self):
            return 0

    def popen(command, **options):
        captured.update(options)
        return Process()

    monkeypatch.setattr("subprocess.Popen", popen)
    start_owned_broker(300.0, 10.0)

    flags = captured["creationflags"]
    assert flags & __import__("subprocess").CREATE_NO_WINDOW
    assert not flags & __import__("subprocess").DETACHED_PROCESS
