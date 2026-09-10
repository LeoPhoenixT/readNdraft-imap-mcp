from __future__ import annotations

import os
import platform
import threading
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from readndraft_imap_mcp import __version__
from readndraft_imap_mcp.broker.limits import RequestQuotaError
from readndraft_imap_mcp.drafts import DraftBusyError, DraftRecoveryRequiredError
from readndraft_imap_mcp.imap.client import ImapClientError, ImapMovePartialError
from readndraft_imap_mcp.mime.html import AuthoredHtmlError
from readndraft_imap_mcp.protocol_version import IPC_PROTOCOL_VERSION

from .codec import (
    RpcError,
    _filters_from_json,
    _identity,
)

if TYPE_CHECKING:
    from readndraft_imap_mcp.mcp_server.backend import ReadOnlyBroker


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


class BrokerRpcServer:
    """Semantic RPC dispatch composed with the local transport runtime."""

    def __init__(
        self,
        broker: ReadOnlyBroker,
        address: str,
        authkey: bytes,
        *,
        idle_timeout_seconds: float | None = None,
        shutdown_grace_seconds: float = 10,
    ) -> None:
        self.broker = broker
        from .transport import BrokerTransportRuntime

        self._transport = BrokerTransportRuntime(
            self.dispatch,
            address,
            authkey,
            idle_timeout_seconds=idle_timeout_seconds,
            shutdown_grace_seconds=shutdown_grace_seconds,
        )

    def handle_frame(self, raw: bytes) -> bytes:
        return self._transport.handle_frame(raw)

    def request_shutdown(self) -> None:
        self._transport.request_shutdown()

    def serve_forever(self) -> None:
        self._transport.serve_forever()

    def _graceful_shutdown(self) -> None:
        self._transport._graceful_shutdown()

    @staticmethod
    def _unix_endpoint_in_use(path: Path) -> bool:
        from .transport import BrokerTransportRuntime

        return BrokerTransportRuntime._unix_endpoint_in_use(path)

    async def dispatch(self, operation: str, params: dict[str, Any]) -> object:
        if operation == "health":
            return {
                "ok": True,
                "status": "healthy",
                "protocol_version": IPC_PROTOCOL_VERSION,
                "package_version": __version__,
                "python_version": platform.python_version(),
                "python_implementation": platform.python_implementation(),
                "pid": os.getpid(),
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
            return [asdict(item) for item in await self.broker.list_mailboxes_batch(tuple(params["account_ids"]))]
        if operation == "search_emails":
            filters = _filters_from_json(params["filters"])
            return [
                asdict(item)
                for item in await self.broker.search_emails(
                    params["account_id"],
                    params["mailbox"],
                    filters,
                    params.get("limit", 50),
                )
            ]
        if operation == "search_email_targets":
            filters = _filters_from_json(params["filters"])
            targets = params["targets"]
            if not isinstance(targets, list) or any(
                not isinstance(item, list) or len(item) != 2 or not all(isinstance(value, str) for value in item)
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
            return asdict(await self.broker.get_email(_identity(params["identity"]), params.get("max_text_chars")))
        if operation == "get_emails":
            return [
                asdict(item)
                for item in await self.broker.get_emails(
                    tuple(_identity(value) for value in params["identities"]),
                    params.get("max_text_chars"),
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
                "to": tuple(params["to"]),
                "cc": tuple(params.get("cc", ())),
                "bcc": tuple(params.get("bcc", ())),
                "subject": params["subject"],
                "body": params["body"],
                "attachment_names": tuple(params.get("attachment_names", ())),
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
            result = await getattr(self.broker, operation)(identity, params["enabled"], params.get("client_id"))
            return asdict(result)
        if operation in {"set_read_state_batch", "set_star_batch"}:
            return [
                asdict(item)
                for item in await getattr(self.broker, operation)(
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
