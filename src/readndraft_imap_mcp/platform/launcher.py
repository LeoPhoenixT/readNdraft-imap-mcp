from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Callable

from readndraft_imap_mcp.audit import JsonlAuditSink
from readndraft_imap_mcp.ipc import IpcBrokerClient

from .broker_compatibility import broker_compatibility
from .paths import AppPaths, current_app_paths

DEFAULT_STARTUP_TIMEOUT = 10.0
DEFAULT_IDLE_TIMEOUT = 300.0
DEFAULT_SHUTDOWN_GRACE = 10.0


class StartupLock:
    """Crash-safe, per-user file lock used only while a broker starts."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("startup lock path must be absolute")
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise PermissionError("startup lock must be a regular file")
        if os.name != "nt":
            metadata = os.fstat(descriptor)
            if metadata.st_uid != os.getuid():
                os.close(descriptor)
                raise PermissionError("startup lock must belong to the current user")
            os.fchmod(descriptor, 0o600)
        self._stream = os.fdopen(descriptor, "r+b", buffering=0)
        if os.fstat(descriptor).st_size == 0:
            try:
                self._stream.write(b"\0")
            except PermissionError:
                # Another Windows launcher may have initialized and locked the
                # byte between our size check and write. Do not read the locked
                # byte; its now-nonzero size is sufficient initialization.
                if os.fstat(descriptor).st_size == 0:
                    self._stream.close()
                    raise
        self._stream.seek(0)
        self.acquired = False

    def try_acquire(self) -> bool:
        if self.acquired:
            return True
        self._stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self.acquired = True
        return True

    def release(self) -> None:
        if not self.acquired:
            return
        self._stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        self.acquired = False

    def close(self) -> None:
        self.release()
        self._stream.close()

    def __enter__(self) -> "StartupLock":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def broker_health(paths: AppPaths, timeout: float = 1.0) -> dict[str, object] | None:
    """Return a bounded authenticated broker health response, if available."""

    finished = threading.Event()
    result: dict[str, object] | None = None

    def check() -> None:
        nonlocal result
        try:
            result = IpcBrokerClient(
                paths.ipc_address, paths.load_or_create_ipc_key()
            ).health()
        except Exception:
            result = None
        finally:
            finished.set()

    threading.Thread(target=check, daemon=True, name="readndraft-health").start()
    return result if finished.wait(timeout) else None


def broker_healthy(paths: AppPaths, timeout: float = 1.0) -> bool:
    """Perform a bounded authenticated health check without exposing errors."""
    result = broker_health(paths, timeout)
    return broker_compatibility(result).compatible


def _instance_released(paths: AppPaths) -> bool:
    with StartupLock(paths.broker_instance_lock_file) as lock:
        return lock.try_acquire()


def retire_stale_broker(paths: AppPaths, timeout: float = 10.0) -> bool:
    """Stop an incompatible broker and wait for endpoint and instance release."""
    result = broker_health(paths)
    if result is not None and broker_compatibility(result).compatible:
        return False
    if result is not None:
        try:
            IpcBrokerClient(paths.ipc_address, paths.load_or_create_ipc_key()).shutdown()
        except Exception:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if broker_health(paths) is None and _instance_released(paths):
            return True
        time.sleep(0.1)
    return False


def start_owned_broker(idle_timeout: float, shutdown_grace: float) -> subprocess.Popen:
    command = [
        sys.executable,
        "-m",
        "readndraft_imap_mcp.broker.daemon",
        "--idle-timeout",
        str(idle_timeout),
        "--shutdown-grace",
        str(shutdown_grace),
    ]
    options: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)
    threading.Thread(target=process.wait, daemon=True, name="readndraft-reaper").start()
    return process


def ensure_broker(
    paths: AppPaths,
    *,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE,
    health_check: Callable[[AppPaths, float], bool] = broker_healthy,
    starter: Callable[[float, float], object] = start_owned_broker,
) -> bool:
    """Ensure one authenticated broker is ready; return whether this call started it."""

    if startup_timeout <= 0 or idle_timeout <= 0 or shutdown_grace < 0:
        raise ValueError("launcher timeouts are invalid")
    paths.ensure_private()
    paths.load_or_create_ipc_key()
    deadline = time.monotonic() + startup_timeout
    if health_check(paths, min(1.0, startup_timeout)):
        return False
    with StartupLock(paths.startup_lock_file) as lock:
        while time.monotonic() < deadline:
            if lock.try_acquire():
                if health_check(paths, min(1.0, max(0.1, deadline - time.monotonic()))):
                    return False
                if not retire_stale_broker(
                    paths, timeout=shutdown_grace + startup_timeout
                ):
                    break
                starter(idle_timeout, shutdown_grace)
                deadline = time.monotonic() + startup_timeout
                while time.monotonic() < deadline:
                    if health_check(
                        paths, min(0.5, max(0.1, deadline - time.monotonic()))
                    ):
                        return True
                    time.sleep(0.05)
                break
            if health_check(paths, min(0.5, max(0.1, deadline - time.monotonic()))):
                return False
            time.sleep(0.05)
    hint = "Run readndraft-imap-mcp doctor for details."
    try:
        JsonlAuditSink(paths.audit_file).verify_sync()
    except (OSError, RuntimeError, ValueError) as exc:
        hint = f"Cause: {exc}. Run readndraft-imap-mcp audit repair, then reconnect."
    raise RuntimeError(
        f"readNdraft broker could not start within {startup_timeout:g} seconds. {hint}"
    )


def _positive(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _non_negative(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start readNdraft MCP with an on-demand broker")
    parser.add_argument("--startup-timeout", type=_positive, default=DEFAULT_STARTUP_TIMEOUT)
    parser.add_argument("--idle-timeout", type=_positive, default=DEFAULT_IDLE_TIMEOUT)
    parser.add_argument("--shutdown-grace", type=_non_negative, default=DEFAULT_SHUTDOWN_GRACE)
    args = parser.parse_args(argv)
    # Import and initialize the comparatively heavy MCP stack before starting an
    # idle-managed broker. On slower Windows hosts, importing it after the
    # health check can consume the entire idle window before a frontend lease is
    # acquired.
    from readndraft_imap_mcp.mcp_server.server import main as mcp_main

    try:
        ensure_broker(
            current_app_paths(),
            startup_timeout=args.startup_timeout,
            idle_timeout=args.idle_timeout,
            shutdown_grace=args.shutdown_grace,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    mcp_main(hold_lease=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
