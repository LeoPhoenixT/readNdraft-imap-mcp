from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from multiprocessing.connection import Client
from typing import Any

from readndraft_imap_mcp.imap.models import (
    AttachmentMetadata,
    BatchFlagChange,
    BatchMessageContent,
    BatchMoveResult,
    DraftCreationResult,
    DraftUpdateResult,
    FlagChange,
    HtmlContent,
    Mailbox,
    MailboxBatchResult,
    MessageContent,
    MessageIdentity,
    MoveResult,
    SearchPage,
    SearchResult,
    SearchTarget,
    SearchTargetError,
    SearchTargetStatus,
)
from readndraft_imap_mcp.safe_error import SafeError

from .codec import (
    _IPC_HELPER_CAPACITY,
    MAX_FRAME_BYTES,
    RPC_RESPONSE_TIMEOUT_SECONDS,
    _encode,
    _filters_to_json,
    _request_frame,
)
from .contract import RpcError

_WRITE_OPERATIONS = frozenset(
    {
        "create_draft",
        "update_draft",
        "set_star",
        "set_read_state",
        "set_star_batch",
        "set_read_state_batch",
        "move_email",
        "move_emails_batch",
    }
)


class IpcBrokerClient:
    def __init__(self, address: str, authkey: bytes) -> None:
        self.address = address
        self.authkey = authkey
        self.family = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"

    def _request_sync(self, operation: str, params: dict[str, Any]) -> Any:
        if not _IPC_HELPER_CAPACITY.acquire(blocking=False):
            raise RpcError(
                "broker request capacity exceeded",
                code="rate_limited",
                scope="client",
                reason="ipc_helper_capacity",
            )
        completed = threading.Event()
        cancelled = threading.Event()
        result: list[Any] = []
        failure: list[BaseException] = []

        def transport() -> None:
            try:
                result.append(self._request_transport(operation, params, cancelled))
            except BaseException as exc:
                failure.append(exc)
            finally:
                completed.set()
                _IPC_HELPER_CAPACITY.release()

        threading.Thread(target=transport, daemon=True, name="readndraft-ipc-client").start()
        if not completed.wait(RPC_RESPONSE_TIMEOUT_SECONDS):
            cancelled.set()
            raise RpcError(
                "broker request timed out",
                code="timeout",
                scope="client",
                reason="request_deadline",
            )
        if failure:
            exc = failure[0]
            if isinstance(exc, RpcError):
                raise exc
            if isinstance(exc, TimeoutError):
                raise RpcError(
                    "broker request timed out",
                    code="timeout",
                    scope="client",
                    reason="request_deadline",
                ) from exc
            if isinstance(exc, (EOFError, OSError)):
                raise RpcError(
                    "broker transport connection failed",
                    code="connection_error",
                    scope="client",
                    reason="transport_loss",
                ) from exc
            raise RpcError("broker request failed", scope="client") from exc
        return result[0]

    def _connect_bounded(self, timeout: float = RPC_RESPONSE_TIMEOUT_SECONDS):
        if not _IPC_HELPER_CAPACITY.acquire(blocking=False):
            raise RpcError(
                "broker request capacity exceeded",
                code="rate_limited",
                scope="client",
                reason="ipc_helper_capacity",
            )
        completed = threading.Event()
        cancelled = threading.Event()
        result: list[Any] = []
        failure: list[BaseException] = []

        def connect() -> None:
            try:
                connection = Client(self.address, family=self.family, authkey=self.authkey)
                if cancelled.is_set():
                    connection.close()
                    raise TimeoutError("broker connection deadline expired")
                result.append(connection)
            except BaseException as exc:
                failure.append(exc)
            finally:
                completed.set()
                _IPC_HELPER_CAPACITY.release()

        threading.Thread(target=connect, daemon=True, name="readndraft-ipc-connect").start()
        if not completed.wait(timeout):
            cancelled.set()
            raise TimeoutError("broker connection deadline expired")
        if failure:
            raise failure[0]
        return result[0]

    def _initialize_lease(self, request: dict[str, Any]):
        if not _IPC_HELPER_CAPACITY.acquire(blocking=False):
            raise RpcError(
                "broker request capacity exceeded",
                code="rate_limited",
                scope="client",
                reason="ipc_helper_capacity",
            )
        deadline = time.monotonic() + RPC_RESPONSE_TIMEOUT_SECONDS
        done = threading.Event()
        cancelled = threading.Event()
        result: list[Any] = []
        failure: list[BaseException] = []

        def initialize() -> None:
            connection = None
            try:
                connection = Client(self.address, family=self.family, authkey=self.authkey)
                if cancelled.is_set():
                    raise TimeoutError("broker frontend lease deadline expired")
                connection.send_bytes(_encode(request))
                remaining = deadline - time.monotonic()
                if cancelled.is_set() or remaining <= 0 or not connection.poll(remaining):
                    raise TimeoutError("broker frontend lease deadline expired")
                response = json.loads(connection.recv_bytes(MAX_FRAME_BYTES).decode("utf-8"))
                if (
                    response.get("request_id") != request["request_id"]
                    or response.get("ok") is not True
                    or response.get("result") != {"leased": True}
                ):
                    raise RpcError("broker frontend lease failed")
                if cancelled.is_set():
                    raise TimeoutError("broker frontend lease deadline expired")
                result.append(connection)
                connection = None
            except BaseException as exc:
                failure.append(exc)
            finally:
                if connection is not None:
                    connection.close()
                done.set()
                _IPC_HELPER_CAPACITY.release()

        threading.Thread(target=initialize, daemon=True, name="readndraft-ipc-lease-init").start()
        if not done.wait(RPC_RESPONSE_TIMEOUT_SECONDS):
            cancelled.set()
            raise TimeoutError("broker frontend lease deadline expired")
        if failure:
            raise failure[0]
        return result[0]

    def _request_transport(self, operation: str, params: dict[str, Any], cancelled: threading.Event) -> Any:
        request = _request_frame(operation, params)
        with Client(self.address, family=self.family, authkey=self.authkey) as connection:
            if cancelled.is_set():
                raise TimeoutError("broker connection deadline expired")
            connection.send_bytes(_encode(request))
            if not connection.poll(RPC_RESPONSE_TIMEOUT_SECONDS):
                raise TimeoutError("broker response deadline expired")
            response = json.loads(connection.recv_bytes(MAX_FRAME_BYTES).decode("utf-8"))
        if response.get("request_id") != request["request_id"]:
            if response.get("ok") is False and response.get("request_id") is None:
                # Broker rejected the frame before it could echo the id; the
                # real error is more useful than a correlation complaint.
                error = response.get("error", {})
                raise RpcError.from_safe_error(_safe_error_from_json(error))
            raise RpcError("broker response request_id mismatch")
        if response.get("ok") is not True:
            error = response.get("error", {})
            raise RpcError.from_safe_error(_safe_error_from_json(error))
        return response["result"]

    async def _request(self, operation: str, params: dict[str, Any]) -> Any:
        try:
            # The entire connect/authenticate/send/receive sequence runs in the
            # worker, so this deadline also covers a blocked handshake.
            return await asyncio.wait_for(
                asyncio.to_thread(self._request_sync, operation, params),
                RPC_RESPONSE_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            if operation in _WRITE_OPERATIONS:
                raise RpcError(
                    "write outcome is unknown; do not retry automatically",
                    code="outcome_unknown",
                    scope="client",
                    reason="request_deadline",
                ) from exc
            raise RpcError(
                "broker request timed out",
                code="timeout",
                scope="client",
                reason="request_deadline",
            ) from exc
        except RpcError as exc:
            if (
                operation in _WRITE_OPERATIONS
                and exc.code in {"timeout", "connection_error"}
                and exc.reason in {None, "request_deadline", "transport_loss"}
            ):
                raise RpcError(
                    "write outcome is unknown; do not retry automatically",
                    code="outcome_unknown",
                    scope=exc.scope,
                    reason=exc.reason or ("request_deadline" if exc.code == "timeout" else "transport_loss"),
                ) from exc
            raise
        except (EOFError, OSError) as exc:
            if operation in _WRITE_OPERATIONS:
                raise RpcError(
                    "write outcome is unknown; do not retry automatically",
                    code="outcome_unknown",
                    scope="client",
                    reason="transport_loss",
                ) from exc
            raise RpcError(
                "mail server connection failed",
                code="connection_error",
                scope="client",
                reason="transport_loss",
            ) from exc

    def list_accounts(self):
        return self._request_sync("list_accounts", {})

    def health(self) -> dict[str, object]:
        return self._request_sync("health", {})

    def shutdown(self) -> None:
        self._request_sync("shutdown", {})

    @contextmanager
    def frontend_lease(self) -> Iterator[None]:
        request = _request_frame("frontend_lease", {})
        connection = self._initialize_lease(request)
        try:
            yield
        finally:
            connection.close()

    async def list_mailboxes_batch(self, account_ids):
        return tuple(
            MailboxBatchResult(
                account_id=item["account_id"],
                ok=item["ok"],
                mailboxes=tuple(Mailbox(**mailbox) for mailbox in item["mailboxes"]),
                error=_optional_safe_error(item["error"]),
            )
            for item in await self._request("list_mailboxes", {"account_ids": list(account_ids)})
        )

    async def search_emails(self, account_id, mailbox, filters, limit=50):
        result = await self._request(
            "search_emails",
            {
                "account_id": account_id,
                "mailbox": mailbox,
                "filters": _filters_to_json(filters),
                "limit": limit,
            },
        )
        return tuple(
            SearchResult(
                identity=MessageIdentity(**item["identity"]),
                headers=item["headers"],
                flags=tuple(item["flags"]),
                size=item["size"],
                received_at=item.get("received_at", ""),
            )
            for item in result
        )

    async def search_email_targets(self, targets, filters, limit=50, cursor=None):
        result = await self._request(
            "search_email_targets",
            {
                "targets": [list(item) for item in targets],
                "filters": _filters_to_json(filters),
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
            errors=tuple(
                SearchTargetError(item["account_id"], item["mailbox"], _safe_error_from_json(item["error"]))
                for item in result["errors"]
            ),
            next_cursor=result["next_cursor"],
            truncated=result["truncated"],
            order=result["order"],
            targets_searched=tuple(SearchTarget(**item) for item in result["targets_searched"]),
            targets_pending=tuple(SearchTarget(**item) for item in result["targets_pending"]),
            target_statuses=tuple(
                SearchTargetStatus(
                    item["account_id"],
                    item["mailbox"],
                    item["status"],
                    cursor=item["cursor"],
                    error=_optional_safe_error(item["error"]),
                )
                for item in result["target_statuses"]
            ),
        )

    async def get_email(self, identity, max_text_chars=None):
        item = await self._request(
            "get_email",
            {"identity": asdict(identity), "max_text_chars": max_text_chars},
        )
        return MessageContent(
            identity=MessageIdentity(**item["identity"]),
            headers=item["headers"],
            text=item["text"],
            flags=tuple(item["flags"]),
            attachments=tuple(AttachmentMetadata(**value) for value in item["attachments"]),
            source_size=item.get("source_size", 0),
            text_total_chars=item.get("text_total_chars", len(item["text"])),
            text_truncated=item.get("text_truncated", False),
        )

    async def get_emails(self, identities, max_text_chars=None):
        items = await self._request(
            "get_emails",
            {
                "identities": [asdict(item) for item in identities],
                "max_text_chars": max_text_chars,
            },
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
        item = await self._request(
            "update_draft",
            {"account_id": account_id, "draft_id": draft_id, **_json_kwargs(kwargs)},
        )
        return DraftUpdateResult(**{**item, "attachment_hashes": tuple(item["attachment_hashes"])})

    async def set_star(self, identity, starred, client_id=None):
        return await self._flag_mutation("set_star", identity, starred, client_id)

    async def set_read_state(self, identity, read, client_id=None):
        return await self._flag_mutation("set_read_state", identity, read, client_id)

    async def _flag_mutation(self, operation, identity, enabled, client_id):
        item = await self._request(
            operation,
            {"identity": asdict(identity), "enabled": enabled, "client_id": client_id},
        )
        return _flag_change(item)

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

    async def move_emails_batch(self, identities, destination_mailbox, client_id=None):
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
        key: (
            list(value) if isinstance(value, tuple) else asdict(value) if isinstance(value, MessageIdentity) else value
        )
        for key, value in kwargs.items()
        if key != "reply_to_message" or value is not None
    }


def _flag_change(item: dict[str, Any]) -> FlagChange:
    return FlagChange(
        identity=MessageIdentity(**item["identity"]),
        state=item["state"],
        enabled=item["enabled"],
        changed=item["changed"],
        old_flags=tuple(item["old_flags"]),
        new_flags=tuple(item["new_flags"]),
    )


def _move_result(item: dict[str, Any]) -> MoveResult:
    destination = item["destination_identity"]
    return MoveResult(
        identity=MessageIdentity(**item["identity"]),
        destination_mailbox=item["destination_mailbox"],
        destination_identity=(MessageIdentity(**destination) if destination is not None else None),
        method=item["method"],
    )


def _batch_move_result(item: dict[str, Any]) -> BatchMoveResult:
    return BatchMoveResult(
        identity=MessageIdentity(**item["identity"]),
        ok=item["ok"],
        move=_move_result(item["move"]) if item["move"] is not None else None,
        error=_optional_safe_error(item["error"]),
    )


def _batch_flag_change(item: dict[str, Any]) -> BatchFlagChange:
    return BatchFlagChange(
        identity=MessageIdentity(**item["identity"]),
        ok=item["ok"],
        change=_flag_change(item["change"]) if item["change"] is not None else None,
        error=_optional_safe_error(item["error"]),
    )


def _message(item: dict[str, Any]) -> MessageContent:
    return MessageContent(
        identity=MessageIdentity(**item["identity"]),
        headers=item["headers"],
        text=item["text"],
        flags=tuple(item["flags"]),
        attachments=tuple(AttachmentMetadata(**value) for value in item["attachments"]),
        source_size=item.get("source_size", 0),
        text_total_chars=item.get("text_total_chars", len(item["text"])),
        text_truncated=item.get("text_truncated", False),
    )


def _batch_message(item: dict[str, Any]) -> BatchMessageContent:
    return BatchMessageContent(
        identity=MessageIdentity(**item["identity"]),
        ok=item["ok"],
        message=_message(item["message"]) if item["message"] is not None else None,
        error=_optional_safe_error(item["error"]),
    )


def _safe_error_from_json(value: object) -> SafeError:
    if not isinstance(value, dict) or set(value) != {
        "code",
        "message",
        "scope",
        "reason",
        "retry_after_seconds",
    }:
        raise RpcError("invalid broker error response")
    try:
        return SafeError(**value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RpcError("invalid broker error response") from exc


def _optional_safe_error(value: object) -> SafeError | None:
    return None if value is None else _safe_error_from_json(value)
