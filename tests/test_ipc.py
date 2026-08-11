import pytest

from readndraft_imap_mcp.poc.ipc import handle_request, run_ipc_probe


def test_ipc_protocol_is_allowlisted() -> None:
    assert handle_request({"operation": "health"}) == {"ok": True, "status": "healthy"}
    assert handle_request({"operation": "raw_imap", "command": "anything"}) == {
        "ok": False,
        "error": "operation_not_allowed",
    }


def test_current_platform_ipc_round_trip() -> None:
    try:
        report = run_ipc_probe()
    except RuntimeError as exc:
        if isinstance(exc.__cause__, PermissionError):
            pytest.skip("test sandbox prohibits local sockets")
        raise
    assert report["round_trip"] is True

