from __future__ import annotations

import pytest

from readndraft_imap_mcp.broker.accounts import AccountConfig, AccountRegistry


def test_broker_account_loader_applies_disable_without_restart() -> None:
    from readndraft_imap_mcp.broker.service import BrokerService

    enabled = [True]
    broker = BrokerService(
        accounts_loader=lambda: AccountRegistry(
            [AccountConfig("work", "imap.example.com", 993, "user@example.com", enabled=enabled[0])]
        )
    )
    assert broker.list_accounts()[0]["enabled"] is True
    enabled[0] = False
    assert broker.list_accounts()[0]["enabled"] is False


def test_account_registry_returns_only_safe_pinned_metadata() -> None:
    account = AccountConfig(
        account_id="personal",
        hostname="mail.example.com",
        port=993,
        username="leo@example.com",
    )
    registry = AccountRegistry([account])

    assert registry.list_safe() == [
        {
            "id": "personal",
            "username": "l***@example.com",
            "host": "mail.example.com",
            "port": 993,
            "enabled": True,
            "sender_address": "leo@example.com",
            "sender_name": None,
        }
    ]
    assert "secret" not in repr(registry.list_safe()).casefold()
    assert registry.require_enabled("personal") is account


def test_account_sender_address_is_optional_validated_metadata() -> None:
    account = AccountConfig(
        "work",
        "imap.example.com",
        993,
        "login@internal.example",
        sender_address="leo@example.com",
    )
    assert account.effective_sender_address == "leo@example.com"
    assert account.safe_metadata()["sender_address"] == "leo@example.com"

    for invalid in (
        "",
        "Leo <leo@example.com>",
        "a@example.com, b@example.com",
        "a@example.com\nBcc: x@example.com",
        "@example.com",
        "a b@example.com",
        "a@example..com",
        "a@example.com (comment)",
        " a@example.com",
        "a@example.com ",
    ):
        with pytest.raises(ValueError, match="sender_address"):
            AccountConfig(
                "work", "imap.example.com", 993, "login", sender_address=invalid
            )


@pytest.mark.parametrize("sender_name", ["Display Name", "山田太郎", "Mary Jane Smith"])
def test_account_sender_name_is_normalized_and_exposed(sender_name: str) -> None:
    account = AccountConfig(
        "work", "imap.example.com", 993, "login@example.com",
        sender_name=f"  {sender_name}  ",
    )
    assert account.sender_name == sender_name
    assert account.safe_metadata()["sender_name"] == sender_name


@pytest.mark.parametrize("sender_name", [None, "", "   "])
def test_account_sender_name_blank_is_none(sender_name: str | None) -> None:
    assert AccountConfig(
        "work", "imap.example.com", 993, "login@example.com",
        sender_name=sender_name,
    ).sender_name is None


@pytest.mark.parametrize("sender_name", ["Name\rBcc: x@example.com", "Name\nBcc: x@example.com"])
def test_account_sender_name_rejects_line_breaks(sender_name: str) -> None:
    with pytest.raises(ValueError, match="sender_name"):
        AccountConfig(
            "work", "imap.example.com", 993, "login@example.com",
            sender_name=sender_name,
        )


def test_disabled_and_unknown_accounts_fail_closed() -> None:
    registry = AccountRegistry(
        [AccountConfig("disabled", "mail.example.com", 993, "x@example.com", enabled=False)]
    )
    with pytest.raises(PermissionError, match="disabled"):
        registry.require_enabled("disabled")
    with pytest.raises(KeyError, match="unknown"):
        registry.require_enabled("missing")


@pytest.mark.parametrize("account_id", ["", "has space", "../escape"])
def test_account_id_is_restricted(account_id: str) -> None:
    with pytest.raises(ValueError, match="account_id"):
        AccountConfig(account_id, "mail.example.com", 993, "x@example.com")
