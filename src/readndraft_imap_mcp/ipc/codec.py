from __future__ import annotations

import json
import secrets
import threading
from dataclasses import asdict
from datetime import date
from typing import TYPE_CHECKING, Any

from readndraft_imap_mcp.imap.models import MessageIdentity, SearchFilters

from .contract import _PARAMETERS, ALLOWED_OPERATIONS

if TYPE_CHECKING:
    pass

MAX_FRAME_BYTES = 8 * 1024 * 1024
RPC_RESPONSE_TIMEOUT_SECONDS = 45
BROKER_REQUEST_TIMEOUT_SECONDS = 30
BROKER_WATCHDOG_TIMEOUT_SECONDS = 35
_IPC_HELPER_CAPACITY = threading.BoundedSemaphore(24)


def _identity(value: object) -> MessageIdentity:
    if not isinstance(value, dict) or set(value) != {
        "account_id",
        "mailbox",
        "uid_validity",
        "uid",
    }:
        raise ValueError("invalid message identity")
    if not all(_string(item) for item in value.values()):
        raise ValueError("invalid message identity")
    return MessageIdentity(**value)


def _filters_from_json(values: dict[str, Any]) -> SearchFilters:
    try:
        return SearchFilters(
            **{
                **values,
                "after": date.fromisoformat(values["after"]) if values.get("after") else None,
                "before": date.fromisoformat(values["before"]) if values.get("before") else None,
            }
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid search filters") from exc


def _filters_to_json(filters: SearchFilters) -> dict[str, Any]:
    values = asdict(filters)
    values["after"] = filters.after.isoformat() if filters.after else None
    values["before"] = filters.before.isoformat() if filters.before else None
    return values


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
    if (
        not isinstance(request_id, str)
        or len(request_id) != 32
        or any(char not in "0123456789abcdef" for char in request_id)
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
    if value["operation"] == "create_draft" and value["params"].get("reply_to_message") is None:
        value["params"].pop("reply_to_message", None)
    return value


def _decode_request(raw: bytes) -> dict[str, Any]:
    return _validate_request(_decode_envelope(raw))


def _string(value: object, *, maximum: int = 4096) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and "\x00" not in value


def _string_list(value: object, *, maximum: int) -> bool:
    return isinstance(value, list) and len(value) <= maximum and all(_string(item) for item in value)


def _date_string(value: object) -> bool:
    if not _string(value, maximum=10) or len(value) != 10 or value[4] != "-" or value[7] != "-":
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _validate_parameter_types(operation: str, params: dict[str, Any]) -> None:
    for key in ("account_id", "mailbox", "destination_mailbox", "attachment_id", "draft_id"):
        if key in params and not _string(params[key]):
            raise ValueError("invalid RPC parameter type")
    if "limit" in params and (
        isinstance(params["limit"], bool) or not isinstance(params["limit"], int) or not 1 <= params["limit"] <= 500
    ):
        raise ValueError("invalid RPC parameter type")
    if "max_text_chars" in params and (
        params["max_text_chars"] is not None
        and (
            isinstance(params["max_text_chars"], bool)
            or not isinstance(params["max_text_chars"], int)
            or not 1 <= params["max_text_chars"] <= 100_000
        )
    ):
        raise ValueError("invalid RPC parameter type")
    if "account_ids" in params and (
        not _string_list(params["account_ids"], maximum=10) or not params["account_ids"]
    ):
        raise ValueError("invalid RPC parameter type")
    if "enabled" in params and not isinstance(params["enabled"], bool):
        raise ValueError("invalid RPC parameter type")
    if "identity" in params:
        _identity(params["identity"])
    if "identities" in params:
        values = params["identities"]
        if not isinstance(values, list) or not 1 <= len(values) <= 50:
            raise ValueError("invalid RPC parameter type")
        for value in values:
            _identity(value)
    if "filters" in params:
        filters = params["filters"]
        filter_keys = {
            "sender",
            "recipient",
            "subject",
            "text",
            "attachment_filename",
            "after",
            "before",
            "read",
            "starred",
        }
        if not isinstance(filters, dict) or set(filters) != filter_keys:
            raise ValueError("invalid RPC parameter type")
        for key in ("sender", "recipient", "subject", "text", "attachment_filename"):
            if filters[key] is not None and not _string(filters[key]):
                raise ValueError("invalid RPC parameter type")
        for key in ("after", "before"):
            if filters[key] is not None and not _date_string(filters[key]):
                raise ValueError("invalid RPC parameter type")
        if any(filters[key] is not None and not isinstance(filters[key], bool) for key in ("read", "starred")):
            raise ValueError("invalid RPC parameter type")
    if "targets" in params:
        targets = params["targets"]
        if (
            not isinstance(targets, list)
            or not 1 <= len(targets) <= 20
            or any(
                not isinstance(target, list) or len(target) != 2 or not all(_string(item) for item in target)
                for target in targets
            )
        ):
            raise ValueError("invalid RPC parameter type")
    if "cursor" in params and params["cursor"] is not None and (
        not _string(params["cursor"], maximum=2048) or not params["cursor"].isascii()
    ):
        raise ValueError("invalid RPC parameter type")
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
    if operation == "create_draft" and "reply_to_message" in params and params["reply_to_message"] is not None:
        _identity(params["reply_to_message"])
    if "client_id" in params and params["client_id"] is not None and not _string(params["client_id"], maximum=256):
        raise ValueError("invalid RPC parameter type")


def _encode(value: object) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_FRAME_BYTES:
        raise ValueError("RPC response exceeds frame limit")
    return raw
