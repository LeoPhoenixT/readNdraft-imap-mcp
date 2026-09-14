"""Versioned, dependency-light IPC wire contract."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from readndraft_imap_mcp.safe_error import SafeError

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
    "list_mailboxes": (frozenset({"account_ids"}), frozenset()),
    "search_emails": (frozenset({"account_id", "mailbox", "filters"}), frozenset({"limit"})),
    "search_email_targets": (frozenset({"targets", "filters"}), frozenset({"limit", "cursor"})),
    "get_email": (frozenset({"identity"}), frozenset({"max_text_chars"})),
    "get_emails": (frozenset({"identities"}), frozenset({"max_text_chars"})),
    "get_email_html": (frozenset({"identity"}), frozenset()),
    "list_attachment_inputs": (frozenset(), frozenset()),
    "save_attachment": (frozenset({"identity", "attachment_id"}), frozenset()),
    "create_draft": (
        frozenset({"account_id", "to", "subject", "body"}),
        frozenset({"cc", "bcc", "html_body", "attachment_names", "reply_to_message", "client_id"}),
    ),
    "update_draft": (
        frozenset({"account_id", "draft_id", "to", "subject", "body"}),
        frozenset({"cc", "bcc", "html_body", "attachment_names", "client_id"}),
    ),
    "set_star": (frozenset({"identity", "enabled"}), frozenset({"client_id"})),
    "set_read_state": (frozenset({"identity", "enabled"}), frozenset({"client_id"})),
    "set_read_state_batch": (frozenset({"identities", "enabled"}), frozenset({"client_id"})),
    "set_star_batch": (frozenset({"identities", "enabled"}), frozenset({"client_id"})),
    "move_email": (frozenset({"identity", "destination_mailbox"}), frozenset({"client_id"})),
    "move_emails_batch": (frozenset({"identities", "destination_mailbox"}), frozenset({"client_id"})),
}


class RpcError(RuntimeError):
    """Safe error returned by the local broker RPC boundary."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "broker_error",
        scope: str = "request",
        reason: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.error = SafeError(  # type: ignore[arg-type]
            code, message, scope, reason, retry_after_seconds
        )
        self.code = code
        self.scope = scope
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"{code}: {message}")

    @classmethod
    def from_safe_error(cls, error: SafeError) -> RpcError:
        return cls(
            error.message,
            code=error.code,
            scope=error.scope,
            reason=error.reason,
            retry_after_seconds=error.retry_after_seconds,
        )


S = {"type": "string"}
INTEGER = {"type": "integer"}
B = {"type": "boolean"}
N = {"type": "null"}


def A(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def object_schema(properties: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additional": False}


O = object_schema  # noqa: E741


def U(schema: dict[str, Any]) -> dict[str, Any]:
    return {"one_of": [schema, N]}


# Exact dictionaries produced by dataclasses.asdict at the IPC boundary.
SCHEMA_DEFINITIONS = {
    "SafeError": O(
        {
            "code": {
                "type": "string",
                "enum": [
                    "partial_move", "permission_denied", "not_found", "invalid_request",
                    "timeout", "rate_limited", "draft_busy", "recovery_required",
                    "imap_error", "connection_error", "broker_error", "outcome_unknown",
                ],
            },
            "message": S,
            "scope": {"type": "string", "enum": ["request", "item", "account", "broker", "client"]},
            "reason": U(
                {
                    "type": "string",
                    "enum": [
                        "task_rate", "session_queue_timeout", "imap_worker_capacity",
                        "ipc_helper_capacity", "request_deadline", "transport_loss",
                    ],
                }
            ),
            "retry_after_seconds": U(INTEGER),
        }
    ),
    "Account": O(
        {"id": S, "username": S, "host": S, "port": INTEGER, "enabled": B, "sender_address": U(S), "sender_name": U(S)}
    ),
    "Mailbox": O({"name": S, "delimiter": U(S), "flags": A(S), "display_name": U(S)}),
    "MessageIdentity": O({"account_id": S, "mailbox": S, "uid_validity": S, "uid": S}),
    "SearchTarget": O({"account_id": S, "mailbox": S}),
    "SearchTargetError": O({"account_id": S, "mailbox": S, "error": {"$ref": "SafeError"}}),
    "SearchTargetStatus": O({
        "account_id": S,
        "mailbox": S,
        "status": {"type": "string", "enum": ["complete", "partial", "error", "pending"]},
        "cursor": U(S),
        "error": U({"$ref": "SafeError"}),
    }),
    "AttachmentMetadata": O(
        {"attachment_id": S, "filename": S, "content_type": S, "size": U(INTEGER), "encoded_size": U(INTEGER)}
    ),
    "InputAttachment": O({"name": S, "size": INTEGER, "sha256": S}),
    "SavedAttachment": O(
        {"saved_name": S, "original_name": S, "content_type": S, "size": INTEGER, "sha256": S, "saved_path": U(S)}
    ),
    "SearchResult": O(
        {
            "identity": {"$ref": "MessageIdentity"},
            "headers": {"type": "object", "values": S},
            "flags": A(S),
            "size": INTEGER,
            "received_at": S,
        }
    ),
    "SearchPage": O(
        {
            "results": A({"$ref": "SearchResult"}),
            "errors": A({"$ref": "SearchTargetError"}),
            "next_cursor": U(S),
            "truncated": B,
            "order": S,
            "targets_searched": A({"$ref": "SearchTarget"}),
            "targets_pending": A({"$ref": "SearchTarget"}),
            "target_statuses": A({"$ref": "SearchTargetStatus"}),
        }
    ),
    "MessageContent": O(
        {
            "identity": {"$ref": "MessageIdentity"},
            "headers": {"type": "object", "values": S},
            "text": S,
            "flags": A(S),
            "attachments": A({"$ref": "AttachmentMetadata"}),
            "source_size": INTEGER,
            "text_total_chars": INTEGER,
            "text_truncated": B,
        }
    ),
    "MailboxBatchResult": O(
        {
            "account_id": S,
            "ok": B,
            "mailboxes": A({"$ref": "Mailbox"}),
            "error": U({"$ref": "SafeError"}),
        }
    ),
    "HtmlContent": O({"identity": {"$ref": "MessageIdentity"}, "html": S, "flags": A(S)}),
    "DraftCreationResult": O(
        {
            "account_id": S,
            "mailbox": S,
            "uid_validity": U(S),
            "uid": U(S),
            "message_id": S,
            "attachment_hashes": A(S),
            "draft_id": U(S),
        }
    ),
    "DraftUpdateResult": O(
        {
            "account_id": S,
            "draft_id": S,
            "mailbox": S,
            "uid_validity": U(S),
            "uid": U(S),
            "message_id": S,
            "attachment_hashes": A(S),
            "method": {"type": "string", "enum": ["replace", "uidplus"]},
        }
    ),
    "FlagChange": O(
        {
            "identity": {"$ref": "MessageIdentity"},
            "state": {"type": "string", "enum": ["starred", "read"]},
            "enabled": B,
            "changed": B,
            "old_flags": A(S),
            "new_flags": A(S),
        }
    ),
    "BatchFlagChange": O(
        {
            "identity": {"$ref": "MessageIdentity"},
            "ok": B,
            "change": U({"$ref": "FlagChange"}),
            "error": U({"$ref": "SafeError"}),
        }
    ),
    "MoveResult": O(
        {
            "identity": {"$ref": "MessageIdentity"},
            "destination_mailbox": S,
            "destination_identity": U({"$ref": "MessageIdentity"}),
            "method": {"type": "string", "enum": ["uid_move", "uidplus_copy_delete"]},
        }
    ),
    "BatchMoveResult": O(
        {
            "identity": {"$ref": "MessageIdentity"},
            "ok": B,
            "move": U({"$ref": "MoveResult"}),
            "error": U({"$ref": "SafeError"}),
        }
    ),
    "BatchMessageContent": O(
        {
            "identity": {"$ref": "MessageIdentity"},
            "ok": B,
            "message": U({"$ref": "MessageContent"}),
            "error": U({"$ref": "SafeError"}),
        }
    ),
}
RESPONSE_SCHEMA_REGISTRY = SCHEMA_DEFINITIONS
RESPONSE_SCHEMAS = {
    "health": O(
        {
            "ok": {"type": "boolean", "enum": [True]},
            "status": {"type": "string", "enum": ["healthy"]},
            "protocol_version": INTEGER,
            "package_version": S,
            "python_version": S,
            "python_implementation": S,
            "pid": INTEGER,
            "resource_limits": O(
                {
                    "task_bucket_capacity": INTEGER,
                    "task_refill_per_second": {"type": "number"},
                    "account_sessions": INTEGER,
                    "imap_workers": INTEGER,
                    "waiting_imap_work": INTEGER,
                }
            ),
            "resource_usage": O(
                {
                    "active_sessions": INTEGER,
                    "queued_session_requests": INTEGER,
                    "rejections": O(
                        {
                            "task_rate": INTEGER,
                            "session_queue_timeout": INTEGER,
                            "imap_worker_capacity": INTEGER,
                        }
                    ),
                }
            ),
        }
    ),
    "shutdown": O({"shutdown": B}),
    "frontend_lease": O({"leased": B}),
    "list_accounts": A({"$ref": "Account"}),
    "list_mailboxes": A({"$ref": "MailboxBatchResult"}),
    "search_emails": A({"$ref": "SearchResult"}),
    "search_email_targets": {"$ref": "SearchPage"},
    "get_email": {"$ref": "MessageContent"},
    "get_emails": A({"$ref": "BatchMessageContent"}),
    "get_email_html": {"$ref": "HtmlContent"},
    "list_attachment_inputs": A({"$ref": "InputAttachment"}),
    "save_attachment": {"$ref": "SavedAttachment"},
    "create_draft": {"$ref": "DraftCreationResult"},
    "update_draft": {"$ref": "DraftUpdateResult"},
    "set_star": {"$ref": "FlagChange"},
    "set_read_state": {"$ref": "FlagChange"},
    "set_read_state_batch": A({"$ref": "BatchFlagChange"}),
    "set_star_batch": A({"$ref": "BatchFlagChange"}),
    "move_email": {"$ref": "MoveResult"},
    "move_emails_batch": A({"$ref": "BatchMoveResult"}),
}


def resolve_response_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Expand named record references for test-side comparison."""
    if "$ref" in schema:
        return resolve_response_schema(SCHEMA_DEFINITIONS[schema["$ref"]])
    return {
        key: [resolve_response_schema(item) if isinstance(item, dict) else item for item in value]
        if isinstance(value, list)
        else resolve_response_schema(value)
        if isinstance(value, dict)
        else value
        for key, value in schema.items()
    }


def canonical_tool_schema_digest(tools: dict[str, dict[str, Any]]) -> str:
    """Hash the stable MCP name/input/output schema contract only."""
    payload = [
        {
            "name": name,
            "inputSchema": tools[name]["inputSchema"],
            "outputSchema": tools[name]["outputSchema"],
        }
        for name in sorted(tools)
    ]
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


_REQUEST_STRING = {
    "type": "string",
    "min_length": 1,
    "max_length": 4096,
    "no_nul": True,
}
_REQUEST_DIGITS = {
    **_REQUEST_STRING,
    "ascii": True,
    "pattern": "^[0-9]+$",
}
_IDENTITY_REQUEST = O(
    {
        "account_id": _REQUEST_STRING,
        "mailbox": _REQUEST_STRING,
        "uid_validity": _REQUEST_DIGITS,
        "uid": _REQUEST_DIGITS,
    }
)
_NULLABLE_REQUEST_STRING = U(_REQUEST_STRING)
_NULLABLE_DATE = U({**_REQUEST_STRING, "format": "date"})
_SEARCH_FILTERS_REQUEST = O(
    {
        "sender": _NULLABLE_REQUEST_STRING,
        "recipient": _NULLABLE_REQUEST_STRING,
        "subject": _NULLABLE_REQUEST_STRING,
        "text": _NULLABLE_REQUEST_STRING,
        "attachment_filename": _NULLABLE_REQUEST_STRING,
        "after": _NULLABLE_DATE,
        "before": _NULLABLE_DATE,
        "read": U(B),
        "starred": U(B),
    }
)
_REQUEST_STRING_100 = {"type": "array", "items": _REQUEST_STRING, "max_items": 100}
_PARAMETER_TYPE_DEFAULTS = {
    "account_id": _REQUEST_STRING,
    "mailbox": _REQUEST_STRING,
    "destination_mailbox": _REQUEST_STRING,
    "attachment_id": _REQUEST_STRING,
    "draft_id": _REQUEST_STRING,
    "client_id": U({**_REQUEST_STRING, "max_length": 256}),
    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
    "max_text_chars": U({"type": "integer", "minimum": 1, "maximum": 100000}),
    "account_ids": {"type": "array", "items": _REQUEST_STRING, "min_items": 1, "max_items": 10},
    "enabled": B,
    "identity": _IDENTITY_REQUEST,
    "identities": {"type": "array", "items": _IDENTITY_REQUEST, "min_items": 1, "max_items": 50},
    "targets": {
        "type": "array",
        "items": {"type": "array", "items": _REQUEST_STRING, "min_items": 2, "max_items": 2},
        "min_items": 1,
        "max_items": 20,
    },
    "filters": _SEARCH_FILTERS_REQUEST,
    "to": _REQUEST_STRING_100,
    "cc": _REQUEST_STRING_100,
    "bcc": _REQUEST_STRING_100,
    "subject": S,
    "body": S,
    "html_body": U(S),
    "attachment_names": {"type": "array", "items": _REQUEST_STRING, "max_items": 25},
    "reply_to_message": U(_IDENTITY_REQUEST),
    "cursor": U({**_REQUEST_STRING, "max_length": 2048, "ascii": True}),
}
REQUEST_SCHEMAS = {
    operation: {
        "required": sorted(required),
        "optional": sorted(optional),
        "additional": False,
        "properties": {key: _PARAMETER_TYPE_DEFAULTS[key] for key in sorted(required | optional)},
    }
    for operation, (required, optional) in _PARAMETERS.items()
}

__all__ = [
    "ALLOWED_OPERATIONS",
    "MAX_FRAME_BYTES",
    "REQUEST_SCHEMAS",
    "RESPONSE_SCHEMA_REGISTRY",
    "RESPONSE_SCHEMAS",
    "SCHEMA_DEFINITIONS",
    "RpcError",
    "_PARAMETERS",
    "canonical_tool_schema_digest",
    "resolve_response_schema",
]
