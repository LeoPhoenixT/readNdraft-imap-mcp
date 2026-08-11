from __future__ import annotations

import asyncio

import pytest

from readndraft_imap_mcp.broker import AccountConfig, AccountRegistry, BrokerService
from readndraft_imap_mcp.imap.models import (
    Mailbox,
    MessageContent,
    MessageIdentity,
    SearchFilters,
    SearchTarget,
)


class FakeCredentialStore:
    async def load_secret(self, account_id: str) -> str:
        assert account_id == "personal"
        return "broker-only-secret"


class FakeClient:
    def __init__(self, account: AccountConfig, secret: str) -> None:
        assert account.hostname == "pinned.example.com"
        assert secret == "broker-only-secret"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def list_mailboxes(self):
        return (Mailbox("INBOX", "/", (r"\HasNoChildren",)),)

    def search(self, mailbox, filters, limit):
        assert (mailbox, filters, limit) == ("INBOX", SearchFilters(), 25)
        return ()


def build_broker() -> BrokerService:
    registry = AccountRegistry(
        [AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]
    )
    return BrokerService(registry, FakeCredentialStore(), FakeClient)


def test_broker_loads_credentials_only_after_pinned_account_resolution() -> None:
    broker = build_broker()
    assert asyncio.run(broker.list_mailboxes("personal")) == (
        Mailbox("INBOX", "/", (r"\HasNoChildren",)),
    )
    assert asyncio.run(
        broker.search_emails("personal", "INBOX", SearchFilters(), limit=25)
    ) == ()


def test_safe_account_listing_does_not_load_credentials() -> None:
    assert build_broker().list_accounts()[0]["username"] == "l***@example.com"


def test_broker_times_out_stalled_credential_backend() -> None:
    class StalledCredentials:
        async def load_secret(self, account_id: str) -> str:
            await asyncio.sleep(1)
            return "too-late"

    broker = BrokerService(
        AccountRegistry(
            [AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]
        ),
        StalledCredentials(),
        FakeClient,
        request_timeout_seconds=0.01,
    )

    with pytest.raises(TimeoutError):
        asyncio.run(broker.list_mailboxes("personal"))


def test_internal_batch_reuses_one_connection_and_isolates_item_errors() -> None:
    created = 0

    class BatchClient(FakeClient):
        def __init__(self, account, secret) -> None:
            nonlocal created
            super().__init__(account, secret)
            created += 1

    broker = BrokerService(
        AccountRegistry(
            [AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]
        ),
        FakeCredentialStore(),
        BatchClient,
    )

    def operation(client, item):
        if item == "missing":
            raise KeyError(item)
        return item.upper()

    outcomes = asyncio.run(
        broker._batch_client_call(
            "personal", ("one", "missing", "two"), operation, max_items=3
        )
    )
    assert created == 1
    assert [(item.value, item.error) for item in outcomes] == [
        ("ONE", None),
        (None, "not_found"),
        ("TWO", None),
    ]


def test_internal_batch_is_bounded() -> None:
    broker = build_broker()
    with pytest.raises(ValueError, match="between 1 and 2"):
        asyncio.run(
            broker._batch_client_call(
                "personal", (1, 2, 3), lambda client, item: item, max_items=2
            )
        )


def test_batch_plain_text_reads_are_ordered_and_share_one_connection() -> None:
    created = 0

    class ReadClient(FakeClient):
        def __init__(self, account, secret) -> None:
            nonlocal created
            super().__init__(account, secret)
            created += 1

        def get_message(self, identity, max_source_bytes):
            assert max_source_bytes > 0
            if identity.uid == "8":
                raise KeyError(identity.uid)
            return MessageContent(identity, {}, f"body {identity.uid}", (), (), 100)

    broker = BrokerService(
        AccountRegistry(
            [AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]
        ),
        FakeCredentialStore(),
        ReadClient,
    )
    identities = tuple(
        MessageIdentity("personal", "INBOX", "42", uid)
        for uid in ("7", "8", "9")
    )

    results = asyncio.run(broker.get_emails(identities))

    assert created == 1
    assert [item.identity.uid for item in results] == ["7", "8", "9"]
    assert [item.ok for item in results] == [True, False, True]
    assert results[1].error == "not_found"


def test_batch_plain_text_read_limits_accounts_and_duplicates() -> None:
    broker = build_broker()
    identity = MessageIdentity("personal", "INBOX", "42", "7")
    with pytest.raises(ValueError, match="unique"):
        asyncio.run(broker.get_emails((identity, identity)))

    identities = tuple(
        MessageIdentity(account, "INBOX", "42", "7")
        for account in ("one", "two", "three")
    )
    with pytest.raises(ValueError, match="2 accounts"):
        asyncio.run(broker.get_emails(identities))


def test_multi_target_search_reuses_one_account_connection() -> None:
    created = 0

    class SearchClient(FakeClient):
        def __init__(self, account, secret) -> None:
            nonlocal created
            super().__init__(account, secret)
            created += 1

        def search(self, mailbox, filters, limit):
            identity = MessageIdentity("personal", mailbox, "42", str(created))
            return (SearchResult(identity, {}, (), 1),)

        def search_window(
            self,
            mailbox,
            filters,
            limit,
            *,
            before_uid=None,
            expected_uid_validity=None,
        ):
            from readndraft_imap_mcp.imap.models import SearchWindow

            return SearchWindow(
                self.search(mailbox, filters, limit), "42", None, False
            )

    from readndraft_imap_mcp.imap.models import SearchResult

    broker = BrokerService(
        AccountRegistry(
            [AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]
        ),
        FakeCredentialStore(),
        SearchClient,
    )
    page = asyncio.run(
        broker.search_email_targets(
            (("personal", "INBOX"), ("personal", "Archive")),
            SearchFilters(),
            10,
        )
    )

    assert created == 1
    assert [item.identity.mailbox for item in page.results] == ["INBOX", "Archive"]
    assert page.errors == ()
    assert page.targets_searched == (
        SearchTarget("personal", "INBOX"),
        SearchTarget("personal", "Archive"),
    )
    assert page.targets_pending == ()


def test_search_cursor_is_bound_and_target_errors_are_isolated() -> None:
    from readndraft_imap_mcp.imap.client import ImapClientError
    from readndraft_imap_mcp.imap.models import SearchResult, SearchWindow

    class SearchClient(FakeClient):
        def search_window(
            self,
            mailbox,
            filters,
            limit,
            *,
            before_uid=None,
            expected_uid_validity=None,
        ):
            if mailbox == "Broken":
                raise ImapClientError("private server detail")
            uids = ("5", "4") if before_uid is None else ("3", "2")
            return SearchWindow(
                tuple(
                    SearchResult(
                        MessageIdentity("personal", mailbox, "42", uid),
                        {},
                        (),
                        1,
                        "2026-08-11T00:00:00Z",
                    )
                    for uid in uids
                ),
                "42",
                uids[-1] if before_uid is None else None,
                before_uid is None,
            )

    broker = BrokerService(
        AccountRegistry(
            [AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]
        ),
        FakeCredentialStore(),
        SearchClient,
    )
    first = asyncio.run(
        broker.search_email_targets(
            (("personal", "INBOX"),), SearchFilters(subject="bound"), 2
        )
    )
    second = asyncio.run(
        broker.search_email_targets(
            (("personal", "INBOX"),),
            SearchFilters(subject="bound"),
            2,
            first.next_cursor,
        )
    )
    mixed = asyncio.run(
        broker.search_email_targets(
            (("personal", "INBOX"), ("personal", "Broken")),
            SearchFilters(),
            10,
        )
    )

    assert first.truncated is True and first.next_cursor is not None
    assert [item.identity.uid for item in second.results] == ["3", "2"]
    assert mixed.errors[0].mailbox == "Broken"
    assert mixed.errors[0].error == "imap_error"
    assert mixed.targets_searched == (
        SearchTarget("personal", "INBOX"),
        SearchTarget("personal", "Broken"),
    )
    assert mixed.targets_pending == ()
    assert "private" not in repr(mixed)

    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(
            broker.search_email_targets(
                (("personal", "INBOX"),),
                SearchFilters(subject="changed"),
                2,
                first.next_cursor,
            )
        )


def test_single_target_search_failure_uses_page_error() -> None:
    from readndraft_imap_mcp.imap.client import ImapClientError

    class FailingSearchClient(FakeClient):
        def search_window(self, mailbox, filters, limit, **kwargs):
            raise ImapClientError("private server detail")

    broker = BrokerService(
        AccountRegistry(
            [AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]
        ),
        FakeCredentialStore(),
        FailingSearchClient,
    )

    page = asyncio.run(
        broker.search_email_targets(
            (("personal", "Infected Items"),), SearchFilters(), 10
        )
    )

    assert page.results == ()
    assert page.errors[0].error == "imap_error"
    assert page.targets_searched == (
        SearchTarget("personal", "Infected Items"),
    )
    assert page.targets_pending == ()
    assert "private" not in repr(page)


def test_search_page_reports_targets_skipped_after_limit_fills() -> None:
    from readndraft_imap_mcp.imap.models import SearchResult, SearchWindow

    class FullSearchClient(FakeClient):
        def search_window(self, mailbox, filters, limit, **kwargs):
            results = tuple(
                SearchResult(
                    MessageIdentity("personal", mailbox, "42", str(index)),
                    {},
                    (),
                    1,
                    "2026-08-11T00:00:00Z",
                )
                for index in range(limit, 0, -1)
            )
            return SearchWindow(results, "42", None, False)

    broker = BrokerService(
        AccountRegistry(
            [AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]
        ),
        FakeCredentialStore(),
        FullSearchClient,
    )

    page = asyncio.run(
        broker.search_email_targets(
            (("personal", "Timesheet"), ("personal", "Leave Application")),
            SearchFilters(),
            3,
        )
    )

    assert len(page.results) == 3
    assert page.targets_searched == (SearchTarget("personal", "Timesheet"),)
    assert page.targets_pending == (
        SearchTarget("personal", "Leave Application"),
    )
    assert page.errors == ()
    assert page.truncated is True
