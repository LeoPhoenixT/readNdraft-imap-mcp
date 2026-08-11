from __future__ import annotations

import os
import secrets
import stat
import sys
import tempfile
import threading
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any


def handle_request(request: object) -> dict[str, Any]:
    if not isinstance(request, dict) or request != {"operation": "health"}:
        return {"ok": False, "error": "operation_not_allowed"}
    return {"ok": True, "status": "healthy"}


def run_ipc_probe() -> dict[str, Any]:
    authkey = secrets.token_bytes(32)
    if sys.platform == "win32":
        address: str = rf"\\.\pipe\readndraft-phase0-{secrets.token_hex(8)}"
        family = "AF_PIPE"
        cleanup = None
    else:
        runtime = Path(tempfile.mkdtemp(prefix="readndraft-phase0-"))
        os.chmod(runtime, 0o700)
        address = str(runtime / "broker.sock")
        family = "AF_UNIX"
        cleanup = runtime

    ready = threading.Event()
    server_error: list[BaseException] = []

    def serve_once() -> None:
        try:
            old_umask = os.umask(0o077)
            try:
                with Listener(address, family=family, authkey=authkey) as listener:
                    if family == "AF_UNIX":
                        os.chmod(address, 0o600)
                    ready.set()
                    with listener.accept() as connection:
                        connection.send(handle_request(connection.recv()))
            finally:
                os.umask(old_umask)
        except BaseException as exc:  # surfaced in the caller thread
            server_error.append(exc)
            ready.set()

    thread = threading.Thread(target=serve_once, daemon=True)
    thread.start()
    if not ready.wait(timeout=5):
        raise TimeoutError("IPC listener did not become ready")
    if server_error:
        raise RuntimeError("IPC listener failed") from server_error[0]

    socket_mode = None
    if family == "AF_UNIX":
        socket_mode = stat.S_IMODE(os.stat(address).st_mode)
        if socket_mode != 0o600:
            raise PermissionError(f"Unix socket mode is {oct(socket_mode)}, expected 0o600")

    with Client(address, family=family, authkey=authkey) as connection:
        connection.send({"operation": "health"})
        response = connection.recv()

    thread.join(timeout=5)
    if thread.is_alive():
        raise TimeoutError("IPC listener did not stop")
    if server_error:
        raise RuntimeError("IPC listener failed") from server_error[0]

    if cleanup is not None:
        Path(address).unlink(missing_ok=True)
        cleanup.rmdir()

    return {
        "platform": sys.platform,
        "family": family,
        "round_trip": response == {"ok": True, "status": "healthy"},
        "unix_socket_mode": oct(socket_mode) if socket_mode is not None else None,
        "note": (
            "Windows named-pipe ACL hardening remains a Phase 0 machine-level check."
            if family == "AF_PIPE"
            else "Unix socket and parent directory are user-only."
        ),
    }


