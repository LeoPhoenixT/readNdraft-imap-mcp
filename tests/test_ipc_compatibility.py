"""Focused coverage of retained module paths, independent of implementation tests."""

from readndraft_imap_mcp.ipc import client, codec, contract, rpc, server


def test_rpc_compatibility_exports_retain_their_implementations() -> None:
    exports = {
        "IpcBrokerClient": client,
        "_json_kwargs": client,
        "BrokerRpcServer": server,
        "_safe_error": server,
        "ALLOWED_OPERATIONS": contract,
        "_PARAMETERS": contract,
        "RpcError": contract,
        "MAX_FRAME_BYTES": codec,
        "RPC_RESPONSE_TIMEOUT_SECONDS": codec,
        "BROKER_REQUEST_TIMEOUT_SECONDS": codec,
        "BROKER_WATCHDOG_TIMEOUT_SECONDS": codec,
        "_decode_request": codec,
        "_encode": codec,
    }
    assert set(rpc.__all__) == set(exports)
    for name, implementation in exports.items():
        assert getattr(rpc, name) is getattr(implementation, name)


def test_broker_service_compatibility_helpers_retain_their_implementations() -> None:
    from readndraft_imap_mcp.broker import drafts, mutations, service

    assert service._reply_thread is drafts.reply_thread
    assert service._mutation_spec is mutations.mutation_spec


def test_imap_session_compatibility_helpers_retain_their_implementations() -> None:
    from readndraft_imap_mcp.imap.client import ImapClient
    from readndraft_imap_mcp.imap.mime_selection import body_candidates, part_message

    assert ImapClient._body_candidates is body_candidates
    assert ImapClient._part_message is part_message
