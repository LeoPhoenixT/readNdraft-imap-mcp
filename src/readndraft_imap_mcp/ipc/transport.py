from __future__ import annotations

import asyncio
import os
import socket
import stat
import sys
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from multiprocessing.connection import AuthenticationError, Client, Listener, answer_challenge, deliver_challenge
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Literal, TypeVar

from readndraft_imap_mcp.broker.common import batch_error
from readndraft_imap_mcp.broker.errors import RequestQuotaError
from readndraft_imap_mcp.broker.settings import DEFAULT_CAPACITY, CapacitySettings
from readndraft_imap_mcp.mime.html import AuthoredHtmlError
from readndraft_imap_mcp.safe_error import SafeError

from .codec import (
    BROKER_WATCHDOG_TIMEOUT_SECONDS,
    MAX_FRAME_BYTES,
    RPC_RESPONSE_TIMEOUT_SECONDS,
    _decode_envelope,
    _encode,
    _validate_request,
)
from .contract import RpcError

if TYPE_CHECKING:
    pass


_DeadlineReason = Literal["handshake_timeout", "frame_timeout", "response_timeout"]
_AbortReason = Literal["handshake_timeout", "frame_timeout", "response_timeout", "shutdown"]
T = TypeVar("T")
_PIPE_CANCELLED_READ = "shouldn't get here; expected KeyboardInterrupt"


@dataclass(slots=True)
class _ConnectionState:
    deadline: float | None
    timeout_reason: _DeadlineReason
    abort_reason: _AbortReason | None = None


class _ConnectionAborted(Exception):
    """A connection already claimed by the broker's watchdog or shutdown."""


def _safe_error(exc: Exception) -> SafeError:
    if isinstance(exc, RpcError):
        return exc.error
    if isinstance(exc, AuthoredHtmlError):
        return SafeError("invalid_request", str(exc), "request")
    return batch_error(exc, scope="request")


class BrokerTransportRuntime:
    def __init__(
        self,
        dispatch: Callable[[str, dict[str, Any]], Coroutine[Any, Any, object]],
        address: str,
        authkey: bytes,
        *,
        idle_timeout_seconds: float | None = None,
        shutdown_grace_seconds: float = 10,
        capacity_settings: CapacitySettings = DEFAULT_CAPACITY,
    ) -> None:
        if idle_timeout_seconds is not None and idle_timeout_seconds <= 0:
            raise ValueError("idle timeout must be positive")
        if shutdown_grace_seconds < 0:
            raise ValueError("shutdown grace must be non-negative")
        self._settings = capacity_settings
        self._connections: dict[object, _ConnectionState] = {}
        self._connection_capacity = threading.BoundedSemaphore(capacity_settings.ipc_connections)
        self._categories = {name: threading.BoundedSemaphore(getattr(capacity_settings, f"ipc_{name}"))
                            for name in ("rpc", "leases", "control")}
        self._category_usage = defaultdict(int)
        self._rejections = defaultdict(int)
        self._deadline_thread: threading.Thread | None = None
        self._shutdown_scheduled = False
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
                and not self._connections
                and self.idle_timeout_seconds is not None
                and time.monotonic() - self._last_activity >= self.idle_timeout_seconds
            )

    def _wake_listener(self) -> None:
        try:
            with Client(self.address, family=self.family, authkey=None):
                pass
        except (AuthenticationError, EOFError, OSError):
            pass

    def request_shutdown(self) -> None:
        with self._state_lock:
            self._shutdown.set()
            connections = []
            for connection, state in self._connections.items():
                if state.abort_reason is None:
                    self._mark_aborted(state, "shutdown")
                    connections.append(connection)
        for connection in connections:
            self._abort_connection(connection)
        if self._runtime_loop is not None and not self._runtime_loop.is_closed():
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

    def schedule_shutdown(self) -> None:
        with self._state_lock:
            if self._shutdown_scheduled:
                return
            self._shutdown_scheduled = True
        threading.Thread(target=self._graceful_shutdown, daemon=True, name="readndraft-shutdown").start()

    def _graceful_shutdown(self) -> None:
        deadline = time.monotonic() + self.shutdown_grace_seconds
        while not self._shutdown.is_set():
            # The acknowledgment has already been sent. Drain every remaining
            # ordinary request/lease before stopping, subject to the grace bound.
            with self._state_lock:
                can_stop = self._active_clients == 0
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
        except Exception as exc:
            return self._error_response(request_id, exc)
        response = self._handle_request(request)
        if request["operation"] == "shutdown":
            self.schedule_shutdown()
        return response

    def _handle_request(self, request: dict[str, Any]) -> bytes:
        """Dispatch an envelope already validated at the frame boundary."""
        request_id = request["request_id"]
        try:
            coroutine = self._dispatch(request["operation"], request["params"])
            if request["operation"] not in {"health", "shutdown", "frontend_lease"}:
                coroutine = asyncio.wait_for(coroutine, BROKER_WATCHDOG_TIMEOUT_SECONDS)
            future = asyncio.run_coroutine_threadsafe(coroutine, self._runtime())
            result = future.result(timeout=RPC_RESPONSE_TIMEOUT_SECONDS)
            return _encode({"request_id": request_id, "ok": True, "result": result})
        except Exception as exc:
            return self._error_response(request_id, exc)

    @staticmethod
    def _error_response(request_id: str | None, exc: Exception) -> bytes:
        return _encode({"request_id": request_id, "ok": False, "error": asdict(_safe_error(exc))})

    def resource_snapshot(self) -> dict[str, object]:
        with self._state_lock:
            return {
                "ipc_connections": len(self._connections),
                "ipc_rpc": self._category_usage["rpc"],
                "ipc_leases": self._category_usage["leases"],
                "ipc_control": self._category_usage["control"],
                "ipc_rejections": {reason: self._rejections[reason] for reason in
                                   ("connection", "rpc", "leases", "control", "handshake_timeout", "frame_timeout")},
            }

    @staticmethod
    def _abort_connection(connection) -> None:
        try:
            if sys.platform == "win32":
                # PipeConnection uses overlapped I/O. Cancel pending operations
                # before closing; a plain poll() cannot bound partial messages.
                import ctypes
                from ctypes import wintypes

                cancel = ctypes.WinDLL("kernel32", use_last_error=True).CancelIoEx
                cancel.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
                cancel.restype = wintypes.BOOL
                cancel(connection.fileno(), None)
                connection.close()
            else:
                # shutdown interrupts blocking reads/writes, including partial
                # multiprocessing frame headers/bodies, without descriptor reuse.
                with socket.socket(fileno=os.dup(connection.fileno())) as duplicate:
                    duplicate.shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError):
            pass

    def _mark_aborted(self, state: _ConnectionState, reason: _AbortReason) -> None:
        """Called under _state_lock; the first abort owns its reason/counter."""
        if state.abort_reason is None:
            state.abort_reason = reason
            if reason in {"handshake_timeout", "frame_timeout"}:
                self._rejections[reason] += 1

    def _expire_connections(self, now: float) -> None:
        with self._state_lock:
            expired = []
            for connection, state in self._connections.items():
                if state.abort_reason is None and state.deadline is not None and state.deadline <= now:
                    # Record the phase before cancelling I/O. A worker cannot
                    # reset this deadline while physical cancellation is pending.
                    reason = "shutdown" if self._shutdown.is_set() else state.timeout_reason
                    self._mark_aborted(state, reason)
                    expired.append(connection)
        for connection in expired:
            self._abort_connection(connection)

    def _connection_deadlines(self) -> None:
        # One watchdog for the whole broker; no per-frame timers/helper threads.
        while not self._shutdown.wait(0.02):
            self._expire_connections(time.monotonic())

    def _check_connection_locked(self, state: _ConnectionState) -> None:
        if state.abort_reason is None:
            if self._shutdown.is_set():
                self._mark_aborted(state, "shutdown")
            elif state.deadline is not None and state.deadline <= time.monotonic():
                self._mark_aborted(state, state.timeout_reason)
        if state.abort_reason is not None:
            raise _ConnectionAborted

    def _check_connection(self, connection) -> None:
        with self._state_lock:
            state = self._connections.get(connection)
            if state is not None:
                self._check_connection_locked(state)

    def _connection_io(self, connection, operation: Callable[[], T]) -> T:
        self._check_connection(connection)
        try:
            return operation()
        except RuntimeError as exc:
            # CPython PipeConnection._recv_bytes can surface overlapped read
            # cancellation (ERROR_OPERATION_ABORTED) as this exact RuntimeError.
            # Normalize only this known I/O outcome after an explicitly recorded
            # broker abort; unrelated RuntimeErrors must remain visible.
            with self._state_lock:
                state = self._connections.get(connection)
                aborted = state is not None and state.abort_reason is not None
            if self.family == "AF_PIPE" and aborted and str(exc) == _PIPE_CANCELLED_READ:
                raise _ConnectionAborted from None
            raise

    def _set_deadline(self, connection, seconds: float | None,
                      reason: _DeadlineReason = "frame_timeout") -> None:
        with self._state_lock:
            state = self._connections.get(connection)
            if state is not None:
                self._check_connection_locked(state)
                state.timeout_reason = reason
                state.deadline = None if seconds is None else time.monotonic() + seconds

    def _accept_connection(self, connection) -> None:
        if not self._connection_capacity.acquire(blocking=False):
            with self._state_lock:
                self._rejections["connection"] += 1
            connection.close()
            return
        with self._state_lock:
            stopping = self._shutdown.is_set()
            if not stopping:
                self._connections[connection] = _ConnectionState(
                    time.monotonic() + self._settings.ipc_handshake_seconds, "handshake_timeout")
        if stopping:
            self._connection_capacity.release()
            connection.close()
            return
        try:
            threading.Thread(target=self._serve_connection, args=(connection,),
                             kwargs={"authenticate": True}, daemon=True, name="readndraft-rpc").start()
        except BaseException:
            with self._state_lock:
                del self._connections[connection]
            self._connection_capacity.release()
            connection.close()
            raise

    def _serve_connection(self, connection, *, authenticate: bool = False) -> None:
        self._client_started()
        category = None
        try:
            with connection:
                if authenticate:
                    # Listener.accept itself never authenticates, so one stalled
                    # peer cannot block accepting unrelated clients.
                    self._connection_io(connection, lambda: deliver_challenge(connection, self.authkey))
                    self._connection_io(connection, lambda: answer_challenge(connection, self.authkey))
                self._set_deadline(connection, self._settings.ipc_frame_seconds)
                raw = self._connection_io(connection, lambda: connection.recv_bytes(MAX_FRAME_BYTES))
                self._check_connection(connection)
                request_id = None
                try:
                    request = _decode_envelope(raw)
                    request_id = request["request_id"]
                    _validate_request(request)
                except Exception as exc:
                    response = self._error_response(request_id, exc)
                    self._connection_io(connection, lambda: connection.send_bytes(response))
                    return
                operation = request["operation"]
                selected = ("leases" if operation == "frontend_lease" else
                            "control" if operation in {"health", "shutdown"} else "rpc")
                if not self._categories[selected].acquire(blocking=False):
                    with self._state_lock:
                        self._rejections[selected] += 1
                    self._connection_io(connection, lambda: connection.send_bytes(self._error_response(
                        request_id, RequestQuotaError("broker IPC capacity exceeded",
                                                      reason="ipc_connection_capacity"))))
                    return
                category = selected
                with self._state_lock:
                    self._category_usage[category] += 1
                self._set_deadline(connection, RPC_RESPONSE_TIMEOUT_SECONDS, "response_timeout")
                if operation == "frontend_lease":
                    self._connection_io(connection, lambda: connection.send_bytes(
                        _encode({"request_id": request_id, "ok": True, "result": {"leased": True}})))
                    self._set_deadline(connection, None, "response_timeout")
                    # Leases need only EOF: poll is sufficient and avoids reading
                    # an attacker-controlled partial frame after registration.
                    if hasattr(connection, "poll"):
                        while not self._shutdown.is_set():
                            if self._connection_io(connection, lambda: connection.poll(0.1)):
                                return
                    else:  # Minimal in-memory transport test adapters.
                        self._connection_io(connection, lambda: connection.recv_bytes(MAX_FRAME_BYTES))
                    return
                response = self._handle_request(request)
                self._connection_io(connection, lambda: connection.send_bytes(response))
                if operation == "shutdown":
                    # Never abort the shutdown connection before its complete
                    # acknowledgment has reached the authenticated caller.
                    self.schedule_shutdown()
        except _ConnectionAborted:
            pass
        except (AuthenticationError, EOFError, OSError, ValueError):
            # Normal disconnects need no error response. A watchdog/shutdown
            # abort already owns its counter; otherwise classify an elapsed
            # deadline under the same lock as phase transitions.
            try:
                self._check_connection(connection)
            except _ConnectionAborted:
                pass
        finally:
            if category is not None:
                self._categories[category].release()
                with self._state_lock:
                    self._category_usage[category] -= 1
            with self._state_lock:
                registered = connection in self._connections
                self._connections.pop(connection, None)
            if registered:
                self._connection_capacity.release()
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
            with Listener(self.address, family=self.family, authkey=None,
                          backlog=self._settings.ipc_connections) as listener:
                if socket_path is not None:
                    os.chmod(socket_path, 0o600)
                    metadata = socket_path.lstat()
                    socket_identity = (metadata.st_dev, metadata.st_ino)
                self._deadline_thread = threading.Thread(target=self._connection_deadlines, daemon=True,
                                                         name="readndraft-ipc-deadlines")
                self._deadline_thread.start()
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
                    self._accept_connection(connection)
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
