import json
from hashlib import sha256

from readndraft_imap_mcp.ipc.rpc import _PARAMETERS, ALLOWED_OPERATIONS
from readndraft_imap_mcp.protocol_version import IPC_PROTOCOL_VERSION

# Bump IPC_PROTOCOL_VERSION and re-pin BOTH values below whenever the IPC
# wire contract changes. A stale broker must never be reachable by a newer
# frontend: the endpoint name is derived from IPC_PROTOCOL_VERSION.
EXPECTED_PROTOCOL_VERSION = 10
EXPECTED_CONTRACT_DIGEST = "58f8d22c4b49855f8828d3609742656f4b38fbb874eb6d8e72f0a44e38554d38"


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
