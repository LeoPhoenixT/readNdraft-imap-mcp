import asyncio
import importlib.util
import json
import subprocess
import sys
from dataclasses import fields, is_dataclass
from hashlib import sha256
from pathlib import Path
from types import UnionType
from typing import Literal, Union, get_args, get_origin, get_type_hints

from mcp.shared.memory import create_connected_server_and_client_session

from readndraft_imap_mcp.attachments import InputAttachment, SavedAttachment
from readndraft_imap_mcp.broker.protocol import HealthResponse
from readndraft_imap_mcp.imap.models import (
    BatchFlagChange,
    BatchMessageContent,
    BatchMoveResult,
    DraftCreationResult,
    DraftUpdateResult,
    FlagChange,
    HtmlContent,
    MailboxBatchResult,
    MessageContent,
    MoveResult,
    SearchPage,
    SearchResult,
)
from readndraft_imap_mcp.ipc.contract import (
    ALLOWED_OPERATIONS as CONTRACT_OPERATIONS,
)
from readndraft_imap_mcp.ipc.contract import (
    REQUEST_SCHEMAS,
    RESPONSE_SCHEMAS,
    SCHEMA_DEFINITIONS,
    canonical_tool_schema_digest,
    resolve_response_schema,
)
from readndraft_imap_mcp.ipc.rpc import _PARAMETERS, ALLOWED_OPERATIONS
from readndraft_imap_mcp.mcp_server.server import create_server
from readndraft_imap_mcp.protocol_version import IPC_PROTOCOL_VERSION

ROOT = Path(__file__).resolve().parents[1]
SMOKE_SPEC = importlib.util.spec_from_file_location("codex_dev_mcp_smoke", ROOT / "scripts" / "codex_dev_mcp_smoke.py")
assert SMOKE_SPEC is not None and SMOKE_SPEC.loader is not None
smoke = importlib.util.module_from_spec(SMOKE_SPEC)
SMOKE_SPEC.loader.exec_module(smoke)

# Bump IPC_PROTOCOL_VERSION and re-pin BOTH values below whenever the IPC
# wire contract changes. A stale broker must never be reachable by a newer
# frontend: the endpoint name is derived from IPC_PROTOCOL_VERSION.
EXPECTED_PROTOCOL_VERSION = 12
EXPECTED_CONTRACT_DIGEST = "0afbb48b43f745aa030a1b4299a276422c80224a18bf5eb16b4e0a06d76c5832"


def _contract_digest() -> str:
    payload = {
        "operations": sorted(ALLOWED_OPERATIONS),
        "parameters": {
            operation: [sorted(required), sorted(optional)]
            for operation, (required, optional) in sorted(_PARAMETERS.items())
        },
        "requests": dict(sorted(REQUEST_SCHEMAS.items())),
        "schema_definitions": dict(sorted(SCHEMA_DEFINITIONS.items())),
        "responses": dict(sorted(RESPONSE_SCHEMAS.items())),
        "resolved_responses": {
            operation: resolve_response_schema(schema) for operation, schema in sorted(RESPONSE_SCHEMAS.items())
        },
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def test_wire_contract_is_pinned_to_protocol_version():
    assert CONTRACT_OPERATIONS == ALLOWED_OPERATIONS
    assert (IPC_PROTOCOL_VERSION, _contract_digest()) == (
        EXPECTED_PROTOCOL_VERSION,
        EXPECTED_CONTRACT_DIGEST,
    ), (
        "The IPC wire contract or protocol version changed. If you changed "
        "ALLOWED_OPERATIONS or _PARAMETERS, you MUST bump "
        "IPC_PROTOCOL_VERSION and re-pin EXPECTED_PROTOCOL_VERSION and "
        "EXPECTED_CONTRACT_DIGEST in this test. Skipping the bump lets a "
        "stale broker serve a newer frontend."
    )


def _model_schema(value):
    origin = get_origin(value)
    if origin in (Union, UnionType):
        non_null = [item for item in get_args(value) if item is not type(None)]
        assert len(non_null) == 1
        return {"one_of": [_model_schema(non_null[0]), {"type": "null"}]}
    if origin is Literal:
        values = list(get_args(value))
        return {"type": _primitive_type(type(values[0])), "enum": values}
    if origin in (list, tuple):
        return {"type": "array", "items": _model_schema(get_args(value)[0])}
    if origin is dict:
        return {"type": "object", "values": _model_schema(get_args(value)[1])}
    if isinstance(value, type) and is_dataclass(value):
        hints = get_type_hints(value)
        properties = {field.name: _model_schema(hints[field.name]) for field in fields(value)}
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additional": False,
        }
    return {"type": _primitive_type(value)}


def _primitive_type(value: type) -> str:
    return {str: "string", int: "integer", float: "number", bool: "boolean", type(None): "null"}[value]


def test_static_response_schemas_match_serialized_dataclass_models() -> None:
    models = {
        "health": HealthResponse,
        "list_mailboxes": list[MailboxBatchResult],
        "search_emails": list[SearchResult],
        "search_email_targets": SearchPage,
        "get_email": MessageContent,
        "get_emails": list[BatchMessageContent],
        "get_email_html": HtmlContent,
        "list_attachment_inputs": list[InputAttachment],
        "save_attachment": SavedAttachment,
        "create_draft": DraftCreationResult,
        "update_draft": DraftUpdateResult,
        "set_star": FlagChange,
        "set_read_state": FlagChange,
        "set_read_state_batch": list[BatchFlagChange],
        "set_star_batch": list[BatchFlagChange],
        "move_email": MoveResult,
        "move_emails_batch": list[BatchMoveResult],
    }
    for operation, model in models.items():
        assert resolve_response_schema(RESPONSE_SCHEMAS[operation]) == _model_schema(model)


def test_static_response_schemas_cover_every_ipc_operation() -> None:
    assert set(RESPONSE_SCHEMAS) == ALLOWED_OPERATIONS
    assert set(RESPONSE_SCHEMAS) == set(REQUEST_SCHEMAS)


def test_request_schemas_define_nested_serialized_parameter_shapes() -> None:
    for operation, (required, optional) in _PARAMETERS.items():
        schema = REQUEST_SCHEMAS[operation]
        assert schema["required"] == sorted(required)
        assert schema["optional"] == sorted(optional)
        assert set(schema["properties"]) == required | optional

    filters = REQUEST_SCHEMAS["search_emails"]["properties"]["filters"]
    assert filters["required"] == [
        "sender",
        "recipient",
        "subject",
        "text",
        "attachment_filename",
        "after",
        "before",
        "read",
        "starred",
    ]
    assert all("one_of" in field for field in filters["properties"].values())
    for key in ("after", "before"):
        assert filters["properties"][key]["one_of"][0]["format"] == "date"
    targets = REQUEST_SCHEMAS["search_email_targets"]["properties"]["targets"]
    assert targets["items"]["type"] == "array"
    assert targets["items"]["min_items"] == targets["items"]["max_items"] == 2
    assert targets["min_items"] == 1
    assert targets["max_items"] == 20
    assert REQUEST_SCHEMAS["search_emails"]["properties"]["limit"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 500,
    }
    assert REQUEST_SCHEMAS["search_email_targets"]["properties"]["cursor"]["one_of"][0] == {
        "type": "string",
        "min_length": 1,
        "max_length": 2048,
        "no_nul": True,
        "ascii": True,
    }
    assert REQUEST_SCHEMAS["create_draft"]["properties"]["reply_to_message"]["one_of"][1] == {"type": "null"}


def test_contract_import_does_not_load_privileged_or_runtime_model_modules() -> None:
    blocked = (
        "readndraft_imap_mcp.broker",
        "readndraft_imap_mcp.credentials",
        "readndraft_imap_mcp.admin",
        "readndraft_imap_mcp.imap",
        "readndraft_imap_mcp.attachments",
    )
    code = "\n".join(
        (
            "import sys",
            "import readndraft_imap_mcp.ipc.contract",
            f"blocked = {blocked!r}",
            "assert not [name for name in sys.modules if name.startswith(blocked)]",
        )
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


async def _connected_tool_schema_digest() -> str:
    server = create_server(object())
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        listed = await session.list_tools()
    tools = {
        tool.name: {
            "inputSchema": tool.inputSchema,
            "outputSchema": tool.outputSchema,
        }
        for tool in listed.tools
    }
    return canonical_tool_schema_digest(tools)


def test_mcp_tool_schema_catalog_is_exactly_pinned() -> None:
    assert asyncio.run(_connected_tool_schema_digest()) == smoke.EXPECTED_TOOL_SCHEMA_DIGEST
