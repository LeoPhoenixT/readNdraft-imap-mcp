from __future__ import annotations

import asyncio
import os
import socket
import stat
import sys
import threading
import time
from multiprocessing.connection import AuthenticationError, Client, Listener
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from readndraft_imap_mcp.broker.limits import RequestQuotaError
from readndraft_imap_mcp.drafts import DraftBusyError, DraftRecoveryRequiredError
from readndraft_imap_mcp.imap.client import ImapClientError, ImapMovePartialError
from readndraft_imap_mcp.mime.html import AuthoredHtmlError

from .codec import (
    BROKER_REQUEST_TIMEOUT_SECONDS,
    MAX_FRAME_BYTES,
    RPC_RESPONSE_TIMEOUT_SECONDS,
    RpcError,
    _decode_envelope,
    _decode_request,
    _encode,
    _validate_request,
)

if TYPE_CHECKING:
    pass


def _safe_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, DraftBusyError):
        return "draft_busy", "draft update is already in progress"
    if isinstance(exc, DraftRecoveryRequiredError):
        return "recovery_required", "draft update recovery is required"
    if isinstance(exc, ImapMovePartialError):
        return "partial_move", "move may have copied the message; inspect both mailboxes"
    if isinstance(exc, PermissionError):
        return "permission_denied", "request denied"
    if isinstance(exc, KeyError):
        return "not_found", "requested resource was not found"
    if isinstance(exc, AuthoredHtmlError):
        return "invalid_request", str(exc)
    if isinstance(exc, (ValueError, RpcError)):
        return "invalid_request", "request rejected"
    if isinstance(exc, TimeoutError):
        return "timeout", "broker request timed out"
    if isinstance(exc, RequestQuotaError):
        return "rate_limited", "account request limit exceeded"
    if isinstance(exc, ImapClientError):
        return "imap_error", "IMAP operation failed"
    if isinstance(exc, OSError):
        return "connection_error", "mail server connection failed"
    return "broker_error", "broker request failed"


class BrokerTransportRuntime:
    def __init__(
        self,
        dispatch: Callable[[str, dict[str, Any]], Coroutine[Any, Any, object]],
        address: str,
        authkey: bytes,
        *,
        idle_timeout_seconds: float | None = None,
        shutdown_grace_seconds: float = 10,
    ) -> None:
        if idle_timeout_seconds is not None and idle_timeout_seconds <= 0:
            raise ValueError("idle timeout must be positive")
        if shutdown_grace_seconds < 0:
            raise ValueError("shutdown grace must be non-negative")
        self._dispatch = dispatch
        self.address = address
        self.authkey = authkey
        self.family = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"
        self.idle_timeout_seconds = idle_timeout_seconds
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self._state_lock = threading.Lock()
        self._active_clients = 0
        self._last_activity = time.monotonic()
        self._shutdown = threading.Event()
        self._runtime_loop: asyncio.AbstractEventLoop | None = None
        self._runtime_started = False
        self._runtime_ready = threading.Event()
        self._runtime_lock = threading.Lock()

    def _runtime(self) -> asyncio.AbstractEventLoop:
        """One broker-owned loop avoids waiting for executor shutdown per RPC."""
        with self._runtime_lock:
            if not self._runtime_started:
                self._runtime_started = True

                def run() -> None:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    self._runtime_loop = loop
                    self._runtime_ready.set()
                    try:
                        loop.run_forever()
                    finally:
                        pending = asyncio.all_tasks(loop)
                        for task in pending:
                            task.cancel()
                        if pending:
                            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                        loop.run_until_complete(loop.shutdown_asyncgens())
                        loop.close()

                threading.Thread(target=run, daemon=True, name="readndraft-rpc-loop").start()
        self._runtime_ready.wait()
        assert self._runtime_loop is not None
        return self._runtime_loop

    def _client_started(self) -> None:
        with self._state_lock:
            self._active_clients += 1
            self._last_activity = time.monotonic()

    def _client_finished(self) -> None:
        with self._state_lock:
            self._active_clients -= 1
            self._last_activity = time.monotonic()

    def _idle(self) -> bool:
        with self._state_lock:
            return (
                self._active_clients == 0
                and self.idle_timeout_seconds is not None
                and time.monotonic() - self._last_activity >= self.idle_timeout_seconds
            )

    def _wake_listener(self) -> None:
        try:
            with Client(self.address, family=self.family, authkey=self.authkey):
                pass
        except (AuthenticationError, EOFError, OSError):
            pass

    def request_shutdown(self) -> None:
        self._shutdown.set()
        if self._runtime_loop is not None:
            self._runtime_loop.call_soon_threadsafe(self._runtime_loop.stop)
        self._wake_listener()

    def _idle_watchdog(self) -> None:
        assert self.idle_timeout_seconds is not None
        interval = min(1.0, max(0.05, self.idle_timeout_seconds / 4))
        while not self._shutdown.wait(interval):
            if not self._idle():
                continue
            if self._shutdown.wait(self.shutdown_grace_seconds):
                return
            if self._idle():
                self.request_shutdown()
                return

    def _graceful_shutdown(self) -> None:
        deadline = time.monotonic() + self.shutdown_grace_seconds
        while not self._shutdown.is_set():
            # The shutdown RPC itself is the sole active client when no work or
            # frontend lease needs draining, so an explicit stop can be prompt.
            with self._state_lock:
                can_stop = self._active_clients <= 1
            if can_stop or time.monotonic() >= deadline:
                self.request_shutdown()
                return
            self._shutdown.wait(0.05)

    def handle_frame(self, raw: bytes) -> bytes:
        request_id = None
        try:
            request = _decode_envelope(raw)
            request_id = request["request_id"]
            _validate_request(request)
            coroutine = self._dispatch(request["operation"], request["params"])
            if request["operation"] not in {"health", "shutdown", "frontend_lease"}:
                coroutine = asyncio.wait_for(coroutine, BROKER_REQUEST_TIMEOUT_SECONDS)
            future = asyncio.run_coroutine_threadsafe(coroutine, self._runtime())
            result = future.result(timeout=RPC_RESPONSE_TIMEOUT_SECONDS)
            return _encode({"request_id": request_id, "ok": True, "result": result})
        except Exception as exc:
            error_type, message = _safe_error(exc)
            return _encode(
                {
                    "request_id": request_id,
                    "ok": False,
                    "error": {"type": error_type, "message": message},
                }
            )

    def _serve_connection(self, connection) -> None:
        self._client_started()
        try:
            with connection:
                raw = connection.recv_bytes(MAX_FRAME_BYTES)
                try:
                    request = _decode_request(raw)
                except ValueError:
                    connection.send_bytes(self.handle_frame(raw))
                    return
                if request["operation"] == "frontend_lease":
                    connection.send_bytes(
                        _encode(
                            {
                                "request_id": request["request_id"],
                                "ok": True,
                                "result": {"leased": True},
                            }
                        )
                    )
                    # The authenticated frontend holds this connection open for
                    # its lifetime. EOF releases the lease and starts the idle
                    # countdown only after the final frontend disconnects.
                    connection.recv_bytes(MAX_FRAME_BYTES)
                    return
                connection.send_bytes(self.handle_frame(raw))
        except (EOFError, OSError):
            pass
        finally:
            self._client_finished()

    @staticmethod
    def _unix_endpoint_in_use(path: Path) -> bool:
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise RuntimeError("broker endpoint path is not a socket")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(path))
        except (ConnectionRefusedError, FileNotFoundError):
            return False
        except (OSError, TimeoutError):
            return True
        finally:
            probe.close()
        return True

    def serve_forever(self) -> None:
        socket_path = Path(self.address) if self.family == "AF_UNIX" else None
        socket_identity: tuple[int, int] | None = None
        if socket_path is not None:
            socket_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(socket_path.parent, 0o700)
            try:
                socket_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if self._unix_endpoint_in_use(socket_path):
                    raise RuntimeError("broker endpoint is already in use")
                socket_path.unlink()
        old_umask = os.umask(0o077)
        try:
            with Listener(self.address, family=self.family, authkey=self.authkey) as listener:
                if socket_path is not None:
                    os.chmod(socket_path, 0o600)
                    metadata = socket_path.lstat()
                    socket_identity = (metadata.st_dev, metadata.st_ino)
                if self.idle_timeout_seconds is not None:
                    threading.Thread(
                        target=self._idle_watchdog,
                        daemon=True,
                        name="readndraft-idle",
                    ).start()
                while not self._shutdown.is_set():
                    try:
                        connection = listener.accept()
                    except (AuthenticationError, EOFError):
                        continue
                    if self._shutdown.is_set():
                        connection.close()
                        break
                    threading.Thread(
                        target=self._serve_connection,
                        args=(connection,),
                        daemon=True,
                        name="readndraft-rpc",
                    ).start()
        finally:
            self._shutdown.set()
            os.umask(old_umask)
            if socket_path is not None and socket_identity is not None:
                try:
                    metadata = socket_path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if (metadata.st_dev, metadata.st_ino) == socket_identity:
                        socket_path.unlink()
