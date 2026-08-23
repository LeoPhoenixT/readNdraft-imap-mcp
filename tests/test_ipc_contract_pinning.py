import json
from hashlib import sha256

from readndraft_imap_mcp.ipc.rpc import _PARAMETERS, ALLOWED_OPERATIONS
from readndraft_imap_mcp.protocol_version import IPC_PROTOCOL_VERSION

# Bump IPC_PROTOCOL_VERSION and re-pin BOTH values below whenever the IPC
# wire contract changes. A stale broker must never be reachable by a newer
# frontend: the endpoint name is derived from IPC_PROTOCOL_VERSION.
EXPECTED_PROTOCOL_VERSION = 9
EXPECTED_CONTRACT_DIGEST = "0422e3e1256f40088da41c0c324815edad008be5354d796671e1d5e437909e88"


def _contract_digest() -> str:
    payload = {
        "operations": sorted(ALLOWED_OPERATIONS),
        "parameters": {
            operation: [sorted(required), sorted(optional)]
            for operation, (required, optional) in sorted(_PARAMETERS.items())
        },
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_wire_contract_is_pinned_to_protocol_version():
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
