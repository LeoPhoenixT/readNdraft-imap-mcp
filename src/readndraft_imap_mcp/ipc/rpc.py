from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import stat
import sys
import threading
import time
from dataclasses import asdict
from datetime import date
from contextlib import contextmanager
from collections.abc import Iterator
from multiprocessing.connection import AuthenticationError, Client, Listener
from pathlib import Path
from typing import Any

from readndraft_imap_mcp import __version__
from readndraft_imap_mcp.broker.service import BrokerService
from readndraft_imap_mcp.broker.limits import RequestQuotaError
from readndraft_imap_mcp.imap.client import ImapClientError, ImapMovePartialError
from readndraft_imap_mcp.imap.models import (
    AttachmentContent,
    AttachmentMetadata,
    BatchFlagChange,
    BatchMoveResult,
    BatchMessageContent,
    DraftCreationResult,
    DraftUpdateResult,
    FlagChange,
    HtmlContent,
    Mailbox,
    MessageContent,
    MessageIdentity,
    MoveResult,
    SearchFilters,
    SearchPage,
    SearchResult,
    SearchTargetError,
    SearchTarget,
)
from readndraft_imap_mcp.mime.html import AuthoredHtmlError
from readndraft_imap_mcp.protocol_version import IPC_PROTOCOL_VERSION

MAX_FRAME_BYTES = 8 * 1024 * 1024
ALLOWED_OPERATIONS = frozenset(
    {
        "health",
        "shutdown",
        "frontend_lease",
        "list_accounts",
        "list_mailboxes",
        "search_emails",
        "search_email_targets",
        "get_email",
        "get_emails",
        "get_email_html",
        "list_attachment_inputs",
        "save_attachment",
        "create_draft",
        "update_draft",
        "set_star",
        "set_read_state",
        "set_read_state_batch",
        "set_star_batch",
        "move_email",
        "move_emails_batch",
    }
)
_PARAMETERS = {
    "health": (frozenset(), frozenset()),
    "shutdown": (frozenset(), frozenset()),
    "frontend_lease": (frozenset(), frozenset()),
    "list_accounts": (frozenset(), frozenset()),
    "list_mailboxes": (frozenset({"account_id"}), frozenset()),
    "search_emails": (
        frozenset({"account_id", "mailbox", "filters"}),
        frozenset({"limit"}),
    ),
    "search_email_targets": (
        frozenset({"targets", "filters"}),
        frozenset({"limit", "cursor"}),
    ),
    "get_email": (frozenset({"identity"}), frozenset()),
    "get_emails": (frozenset({"identities"}), frozenset()),
    "get_email_html": (frozenset({"identity"}), frozenset()),
    "list_attachment_inputs": (frozenset(), frozenset()),
    "save_attachment": (
        frozenset({"identity", "attachment_id"}),
        frozenset(),
    ),
    "create_draft": (
        frozenset({"account_id", "to", "subject", "body"}),
        frozenset({"cc", "bcc", "html_body", "attachment_names", "reply_to_message", "client_id"}),
    ),
    "update_draft": (
        frozenset({"account_id", "draft_id", "to", "subject", "body"}),
        frozenset({"cc", "bcc", "html_body", "attachment_names", "client_id"}),
    ),
    "set_star": (
        frozenset({"identity", "enabled"}),
        frozenset({"client_id"}),
    ),
    "set_read_state": (
        frozenset({"identity", "enabled"}),
        frozenset({"client_id"}),
    ),
    "set_read_state_batch": (
        frozenset({"identities", "enabled"}),
        frozenset({"client_id"}),
    ),
    "set_star_batch": (
        frozenset({"identities", "enabled"}),
        frozenset({"client_id"}),
    ),
    "move_email": (
        frozenset({"identity", "destination_mailbox"}),
        frozenset({"client_id"}),
    ),
    "move_emails_batch": (
        frozenset({"identities", "destination_mailbox"}),
        frozenset({"client_id"}),
    ),
}


class RpcError(RuntimeError):
    """Safe error returned by the local broker RPC boundary."""

    def __init__(self, message: str, *, code: str = "broker_error") -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _safe_error(exc: Exception) -> tuple[str, str]:
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


def _identity(value: object) -> MessageIdentity:
    if not isinstance(value, dict) or set(value) != {
        "account_id",
        "mailbox",
        "uid_validity",
        "uid",
    }:
        raise ValueError("invalid message identity")
    return MessageIdentity(**value)


def _request_frame(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    if operation not in ALLOWED_OPERATIONS:
        raise ValueError("operation is not allowed")
    return {"request_id": secrets.token_hex(16), "operation": operation, "params": params}


def _decode_envelope(raw: bytes) -> dict[str, Any]:
    """Validate the transport envelope only. Never inspects params."""
    if len(raw) > MAX_FRAME_BYTES:
        raise ValueError("RPC request exceeds frame limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid RPC JSON") from exc
    if not isinstance(value, dict) or set(value) != {"request_id", "operation", "params"}:
        raise ValueError("invalid RPC request shape")
    request_id = value["request_id"]
    if not isinstance(request_id, str) or len(request_id) != 32 or any(
        char not in "0123456789abcdef" for char in request_id
    ):
        raise ValueError("invalid RPC request_id")
    return value


def _validate_request(value: dict[str, Any]) -> dict[str, Any]:
    if value["operation"] not in ALLOWED_OPERATIONS or not isinstance(value["params"], dict):
        raise ValueError("operation is not allowed")
    required, optional = _PARAMETERS[value["operation"]]
    supplied = frozenset(value["params"])
    if not required <= supplied or supplied - required - optional:
        raise ValueError("invalid RPC parameters")
    _validate_parameter_types(value["operation"], value["params"])
    return value


def _decode_request(raw: bytes) -> dict[str, Any]:
    return _validate_request(_decode_envelope(raw))


def _string(value: object, *, maximum: int = 4096) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and "\x00" not in value


def _string_list(value: object, *, maximum: int) -> bool:
    return isinstance(value, list) and len(value) <= maximum and all(_string(item) for item in value)


def _validate_parameter_types(operation: str, params: dict[str, Any]) -> None:
    for key in (
        "account_id", "mailbox", "destination_mailbox", "attachment_id", "draft_id"
    ):
        if key in params and not _string(params[key]):
            raise ValueError("invalid RPC parameter type")
    if "limit" in params and (
        isinstance(params["limit"], bool) or not isinstance(params["limit"], int)
    ):
        raise ValueError("invalid RPC parameter type")
    if "enabled" in params and not isinstance(params["enabled"], bool):
        raise ValueError("invalid RPC parameter type")
    if "identity" in params:
        _identity(params["identity"])
    if "identities" in params:
        values = params["identities"]
        if not isinstance(values, list) or len(values) > 50:
            raise ValueError("invalid RPC parameter type")
        for value in values:
            _identity(value)
    if operation in {"create_draft", "update_draft"}:
        if not _string_list(params["to"], maximum=100):
            raise ValueError("invalid RPC parameter type")
        for key in ("cc", "bcc"):
            if key in params and not _string_list(params[key], maximum=100):
                raise ValueError("invalid RPC parameter type")
        if not isinstance(params["subject"], str) or not isinstance(params["body"], str):
            raise ValueError("invalid RPC parameter type")
        if "html_body" in params and params["html_body"] is not None and not isinstance(params["html_body"], str):
            raise ValueError("invalid RPC parameter type")
        if "attachment_names" in params and not _string_list(params["attachment_names"], maximum=25):
            raise ValueError("invalid RPC parameter type")
    if operation == "create_draft" and "reply_to_message" in params:
        _identity(params["reply_to_message"])
    if "client_id" in params and params["client_id"] is not None and not _string(params["client_id"], maximum=256):
        raise ValueError("invalid RPC parameter type")


def _encode(value: object) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_FRAME_BYTES:
        raise ValueError("RPC response exceeds frame limit")
    return raw


class BrokerRpcServer:
    def __init__(
        self,
        broker: BrokerService,
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
        self.broker = broker
        self.address = address
        self.authkey = authkey
        self.family = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"
        self.idle_timeout_seconds = idle_timeout_seconds
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self._state_lock = threading.Lock()
        self._active_clients = 0
        self._last_activity = time.monotonic()
        self._shutdown = threading.Event()

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

    async def dispatch(self, operation: str, params: dict[str, Any]) -> object:
        if operation == "health":
            return {
                "ok": True,
                "status": "healthy",
                "protocol_version": IPC_PROTOCOL_VERSION,
                "package_version": __version__,
            }
        if operation == "shutdown":
            threading.Thread(
                target=self._graceful_shutdown,
                daemon=True,
                name="readndraft-shutdown",
            ).start()
            return {"shutdown": True}
        if operation == "list_accounts":
            return self.broker.list_accounts()
        if operation == "list_mailboxes":
            return [asdict(item) for item in await self.broker.list_mailboxes(params["account_id"])]
        if operation == "search_emails":
            values = params["filters"]
            filters = SearchFilters(
                **{
                    **values,
                    "after": date.fromisoformat(values["after"]) if values.get("after") else None,
                    "before": date.fromisoformat(values["before"]) if values.get("before") else None,
                }
            )
            return [
                asdict(item)
                for item in await self.broker.search_emails(
                    params["account_id"], params["mailbox"], filters,
                    params.get("limit", 50),
                )
            ]
        if operation == "search_email_targets":
            values = params["filters"]
            filters = SearchFilters(
                **{
                    **values,
                    "after": date.fromisoformat(values["after"]) if values.get("after") else None,
                    "before": date.fromisoformat(values["before"]) if values.get("before") else None,
                }
            )
            targets = params["targets"]
            if not isinstance(targets, list) or any(
                not isinstance(item, list)
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
                for item in targets
            ):
                raise ValueError("invalid search targets")
            return asdict(
                await self.broker.search_email_targets(
                    tuple((item[0], item[1]) for item in targets),
                    filters,
                    params.get("limit", 50),
                    params.get("cursor"),
                )
            )
        if operation == "get_email":
            return asdict(await self.broker.get_email(_identity(params["identity"])))
        if operation == "get_emails":
            return [
                asdict(item)
                for item in await self.broker.get_emails(
                    tuple(_identity(value) for value in params["identities"])
                )
            ]
        if operation == "get_email_html":
            return asdict(await self.broker.get_email_html(_identity(params["identity"])))
        if operation == "list_attachment_inputs":
            return [asdict(item) for item in self.broker.list_attachment_inputs()]
        if operation == "save_attachment":
            return asdict(await self.broker.save_attachment(_identity(params["identity"]), params["attachment_id"]))
        if operation in {"create_draft", "update_draft"}:
            kwargs = {
                "to": tuple(params["to"]), "cc": tuple(params.get("cc", ())),
                "bcc": tuple(params.get("bcc", ())), "subject": params["subject"],
                "body": params["body"], "attachment_names": tuple(params.get("attachment_names", ())),
                "html_body": params.get("html_body"),
                "client_id": params.get("client_id"),
            }
            if operation == "create_draft":
                if "reply_to_message" in params:
                    kwargs["reply_to_message"] = _identity(params["reply_to_message"])
                return asdict(await self.broker.create_draft(params["account_id"], **kwargs))
            return asdict(await self.broker.update_draft(params["account_id"], params["draft_id"], **kwargs))
        if operation in {"set_star", "set_read_state"}:
            identity = _identity(params["identity"])
            if operation == "set_star":
                result = await self.broker.set_star(identity, params["enabled"], params.get("client_id"))
            else:
                result = await self.broker.set_read_state(identity, params["enabled"], params.get("client_id"))
            return asdict(result)
        if operation == "set_read_state_batch":
            return [
                asdict(item)
                for item in await self.broker.set_read_state_batch(
                    tuple(_identity(value) for value in params["identities"]),
                    params["enabled"],
                    params.get("client_id"),
                )
            ]
        if operation == "set_star_batch":
            return [
                asdict(item)
                for item in await self.broker.set_star_batch(
                    tuple(_identity(value) for value in params["identities"]),
                    params["enabled"],
                    params.get("client_id"),
                )
            ]
        if operation == "move_email":
            return asdict(
                await self.broker.move_email(
                    _identity(params["identity"]),
                    params["destination_mailbox"],
                    params.get("client_id"),
                )
            )
        if operation == "move_emails_batch":
            return [
                asdict(item)
                for item in await self.broker.move_emails_batch(
                    tuple(_identity(value) for value in params["identities"]),
                    params["destination_mailbox"],
                    params.get("client_id"),
                )
            ]
        raise ValueError("operation is not allowed")

    def _graceful_shutdown(self) -> None:
        if self._shutdown.wait(self.shutdown_grace_seconds):
            return
        self.request_shutdown()

    def handle_frame(self, raw: bytes) -> bytes:
        request_id = None
        try:
            request = _decode_envelope(raw)
            request_id = request["request_id"]
            _validate_request(request)
            result = asyncio.run(self.dispatch(request["operation"], request["params"]))
            return _encode({"request_id": request_id, "ok": True, "result": result})
        except Exception as exc:
            error_type, message = _safe_error(exc)
            return _encode({
                "request_id": request_id, "ok": False,
                "error": {"type": error_type, "message": message},
            })

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


class IpcBrokerClient:
    """MCP-side semantic broker client. It never handles IMAP credentials."""

    def __init__(self, address: str, authkey: bytes) -> None:
        self.address = address
        self.authkey = authkey
        self.family = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"

    def _request_sync(self, operation: str, params: dict[str, Any]) -> Any:
        request = _request_frame(operation, params)
        with Client(self.address, family=self.family, authkey=self.authkey) as connection:
            connection.send_bytes(_encode(request))
            response = json.loads(connection.recv_bytes(MAX_FRAME_BYTES).decode("utf-8"))
        if response.get("request_id") != request["request_id"]:
            if response.get("ok") is False and response.get("request_id") is None:
                # Broker rejected the frame before it could echo the id; the
                # real error is more useful than a correlation complaint.
                error = response.get("error", {})
                raise RpcError(
                    str(error.get("message", "broker request failed")),
                    code=str(error.get("type", "broker_error")),
                )
            raise RpcError("broker response request_id mismatch")
        if response.get("ok") is not True:
            error = response.get("error", {})
            raise RpcError(
                str(error.get("message", "broker request failed")),
                code=str(error.get("type", "broker_error")),
            )
        return response["result"]

    async def _request(self, operation: str, params: dict[str, Any]) -> Any:
        return await asyncio.to_thread(self._request_sync, operation, params)

    def list_accounts(self):
        return self._request_sync("list_accounts", {})

    def health(self) -> dict[str, object]:
        return self._request_sync("health", {})

    def shutdown(self) -> None:
        self._request_sync("shutdown", {})

    @contextmanager
    def frontend_lease(self) -> Iterator[None]:
        request = _request_frame("frontend_lease", {})
        connection = Client(self.address, family=self.family, authkey=self.authkey)
        try:
            connection.send_bytes(_encode(request))
            response = json.loads(
                connection.recv_bytes(MAX_FRAME_BYTES).decode("utf-8")
            )
            if response.get("request_id") != request["request_id"]:
                if response.get("ok") is False and response.get("request_id") is None:
                    # Broker rejected the frame before it could echo the id; the
                    # real error is more useful than a correlation complaint.
                    error = response.get("error", {})
                    raise RpcError(
                        str(error.get("message", "broker request failed")),
                        code=str(error.get("type", "broker_error")),
                    )
                raise RpcError("broker response request_id mismatch")
            if response.get("ok") is not True or response.get("result") != {
                "leased": True
            }:
                raise RpcError("broker frontend lease failed")
            yield
        finally:
            connection.close()

    async def list_mailboxes(self, account_id):
        return tuple(Mailbox(**item) for item in await self._request("list_mailboxes", {"account_id": account_id}))

    async def search_emails(self, account_id, mailbox, filters, limit=50):
        values = asdict(filters)
        values["after"] = filters.after.isoformat() if filters.after else None
        values["before"] = filters.before.isoformat() if filters.before else None
        result = await self._request("search_emails", {
            "account_id": account_id, "mailbox": mailbox, "filters": values,
            "limit": limit,
        })
        return tuple(SearchResult(
            identity=MessageIdentity(**item["identity"]), headers=item["headers"],
            flags=tuple(item["flags"]), size=item["size"],
            received_at=item.get("received_at", ""),
        ) for item in result)

    async def search_email_targets(self, targets, filters, limit=50, cursor=None):
        values = asdict(filters)
        values["after"] = filters.after.isoformat() if filters.after else None
        values["before"] = filters.before.isoformat() if filters.before else None
        result = await self._request(
            "search_email_targets",
            {
                "targets": [list(item) for item in targets],
                "filters": values,
                "limit": limit,
                "cursor": cursor,
            },
        )
        return SearchPage(
            results=tuple(
                SearchResult(
                    identity=MessageIdentity(**item["identity"]),
                    headers=item["headers"],
                    flags=tuple(item["flags"]),
                    size=item["size"],
                    received_at=item["received_at"],
                )
                for item in result["results"]
            ),
            errors=tuple(SearchTargetError(**item) for item in result["errors"]),
            next_cursor=result["next_cursor"],
            truncated=result["truncated"],
            order=result["order"],
            targets_searched=tuple(
                SearchTarget(**item) for item in result["targets_searched"]
            ),
            targets_pending=tuple(
                SearchTarget(**item) for item in result["targets_pending"]
            ),
        )

    async def get_email(self, identity):
        item = await self._request("get_email", {"identity": asdict(identity)})
        return MessageContent(
            identity=MessageIdentity(**item["identity"]), headers=item["headers"], text=item["text"],
            flags=tuple(item["flags"]),
            attachments=tuple(AttachmentMetadata(**value) for value in item["attachments"]),
            source_size=item.get("source_size", 0),
        )

    async def get_emails(self, identities):
        items = await self._request(
            "get_emails", {"identities": [asdict(item) for item in identities]}
        )
        return tuple(_batch_message(item) for item in items)

    async def get_email_html(self, identity):
        item = await self._request("get_email_html", {"identity": asdict(identity)})
        return HtmlContent(MessageIdentity(**item["identity"]), item["html"], tuple(item["flags"]))

    def list_attachment_inputs(self):
        from readndraft_imap_mcp.attachments import InputAttachment
        return tuple(InputAttachment(**item) for item in self._request_sync("list_attachment_inputs", {}))

    async def save_attachment(self, identity, attachment_id):
        from readndraft_imap_mcp.attachments import SavedAttachment
        item = await self._request("save_attachment", {"identity": asdict(identity), "attachment_id": attachment_id})
        return SavedAttachment(**item)

    async def create_draft(self, account_id, **kwargs):
        item = await self._request("create_draft", {"account_id": account_id, **_json_kwargs(kwargs)})
        return DraftCreationResult(**{**item, "attachment_hashes": tuple(item["attachment_hashes"])})

    async def update_draft(self, account_id, draft_id, **kwargs):
        item = await self._request("update_draft", {"account_id": account_id, "draft_id": draft_id, **_json_kwargs(kwargs)})
        return DraftUpdateResult(**{**item, "attachment_hashes": tuple(item["attachment_hashes"])})

    async def set_star(self, identity, starred, client_id=None):
        return _flag_change(await self._request("set_star", {"identity": asdict(identity), "enabled": starred, "client_id": client_id}))

    async def set_read_state(self, identity, read, client_id=None):
        return _flag_change(await self._request("set_read_state", {"identity": asdict(identity), "enabled": read, "client_id": client_id}))

    async def set_read_state_batch(self, identities, read, client_id=None):
        items = await self._request(
            "set_read_state_batch",
            {
                "identities": [asdict(item) for item in identities],
                "enabled": read,
                "client_id": client_id,
            },
        )
        return tuple(_batch_flag_change(item) for item in items)

    async def set_star_batch(self, identities, starred, client_id=None):
        items = await self._request(
            "set_star_batch",
            {
                "identities": [asdict(item) for item in identities],
                "enabled": starred,
                "client_id": client_id,
            },
        )
        return tuple(_batch_flag_change(item) for item in items)

    async def move_email(self, identity, destination_mailbox, client_id=None):
        item = await self._request(
            "move_email",
            {
                "identity": asdict(identity),
                "destination_mailbox": destination_mailbox,
                "client_id": client_id,
            },
        )
        return _move_result(item)

    async def move_emails_batch(
        self, identities, destination_mailbox, client_id=None
    ):
        items = await self._request(
            "move_emails_batch",
            {
                "identities": [asdict(item) for item in identities],
                "destination_mailbox": destination_mailbox,
                "client_id": client_id,
            },
        )
        return tuple(_batch_move_result(item) for item in items)


def _json_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (list(value) if isinstance(value, tuple) else asdict(value) if isinstance(value, MessageIdentity) else value)
        for key, value in kwargs.items()
        if key != "reply_to_message" or value is not None
    }


def _flag_change(item: dict[str, Any]) -> FlagChange:
    return FlagChange(
        identity=MessageIdentity(**item["identity"]), state=item["state"], enabled=item["enabled"],
        changed=item["changed"], old_flags=tuple(item["old_flags"]), new_flags=tuple(item["new_flags"]),
    )


def _move_result(item: dict[str, Any]) -> MoveResult:
    destination = item["destination_identity"]
    return MoveResult(
        identity=MessageIdentity(**item["identity"]),
        destination_mailbox=item["destination_mailbox"],
        destination_identity=(
            MessageIdentity(**destination) if destination is not None else None
        ),
        method=item["method"],
    )


def _batch_move_result(item: dict[str, Any]) -> BatchMoveResult:
    return BatchMoveResult(
        identity=MessageIdentity(**item["identity"]),
        ok=item["ok"],
        move=_move_result(item["move"]) if item["move"] is not None else None,
        error=item["error"],
    )


def _batch_flag_change(item: dict[str, Any]) -> BatchFlagChange:
    return BatchFlagChange(
        identity=MessageIdentity(**item["identity"]),
        ok=item["ok"],
        change=_flag_change(item["change"]) if item["change"] is not None else None,
        error=item["error"],
    )


def _message(item: dict[str, Any]) -> MessageContent:
    return MessageContent(
        identity=MessageIdentity(**item["identity"]),
        headers=item["headers"],
        text=item["text"],
        flags=tuple(item["flags"]),
        attachments=tuple(AttachmentMetadata(**value) for value in item["attachments"]),
        source_size=item.get("source_size", 0),
    )


def _batch_message(item: dict[str, Any]) -> BatchMessageContent:
    return BatchMessageContent(
        identity=MessageIdentity(**item["identity"]),
        ok=item["ok"],
        message=_message(item["message"]) if item["message"] is not None else None,
        error=item["error"],
    )
