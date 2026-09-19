from __future__ import annotations

import asyncio
import threading
import time

import pytest

from readndraft_imap_mcp.broker import AccountConfig, AccountRegistry, BrokerService
from readndraft_imap_mcp.broker.limits import RequestQuotaError
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


def test_credential_and_imap_share_one_request_deadline() -> None:
    class SlowCredentials:
        async def load_secret(self, account_id):
            await asyncio.sleep(0.04)
            return "broker-only-secret"

    class SlowClient(FakeClient):
        def list_mailboxes(self):
            time.sleep(0.05)
            return super().list_mailboxes()

    broker = BrokerService(
        AccountRegistry([AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]),
        SlowCredentials(), SlowClient, request_timeout_seconds=0.06,
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(broker.list_mailboxes("personal"))
    assert time.monotonic() - started < 0.08


def test_timed_out_worker_holds_the_only_slot_until_it_really_finishes() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingClient(FakeClient):
        def list_mailboxes(self):
            entered.set()
            assert release.wait(timeout=1)
            return super().list_mailboxes()

    broker = BrokerService(
        AccountRegistry([AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]),
        FakeCredentialStore(), BlockingClient, request_timeout_seconds=0.05,
        max_imap_workers=1, max_waiting_imap_work=0,
    )
    with pytest.raises(TimeoutError):
        asyncio.run(broker.list_mailboxes("personal"))
    assert entered.is_set()
    with pytest.raises(RequestQuotaError) as rejected:
        asyncio.run(broker.list_mailboxes("personal"))
    assert rejected.value.reason == "imap_worker_capacity"
    release.set()
    time.sleep(0.1)
    assert asyncio.run(broker.list_mailboxes("personal"))[0].name == "INBOX"


def test_expired_batch_does_not_start_the_next_imap_command() -> None:
    called: list[str] = []
    broker = BrokerService(
        AccountRegistry([AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]),
        FakeCredentialStore(), FakeClient, request_timeout_seconds=0.02,
    )

    def operation(client, value):
        called.append(value)
        if value == "first":
            time.sleep(0.06)
        return value

    outcomes = asyncio.run(
        broker._batch_client_call("personal", ("first", "second"), operation, max_items=2)
    )
    assert called == ["first"]
    assert outcomes[0].value == "first"
    assert outcomes[1].error is not None and outcomes[1].error.code == "timeout"


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
    assert [(item.value, item.error.code if item.error else None) for item in outcomes] == [
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
    assert results[1].error is not None and results[1].error.code == "not_found"


def test_plain_text_preview_is_truncated_before_batch_budgeting() -> None:
    class ReadClient(FakeClient):
        def get_message(self, identity, max_source_bytes=50 * 1024 * 1024):
            return MessageContent(identity, {}, "abcdef", (), (), 100)

    broker = BrokerService(
        AccountRegistry(
            [AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]
        ),
        FakeCredentialStore(),
        ReadClient,
    )
    identity = MessageIdentity("personal", "INBOX", "42", "7")
    message = asyncio.run(broker.get_email(identity, max_text_chars=3))
    assert (message.text, message.text_total_chars, message.text_truncated) == (
        "abc", 6, True
    )
    batch = asyncio.run(broker.get_emails((identity,), max_text_chars=3))
    assert batch[0].message is not None
    assert batch[0].message.text == "abc"
    with pytest.raises(ValueError, match="max_text_chars"):
        asyncio.run(broker.get_email(identity, max_text_chars=0))


def test_mailbox_batch_preserves_order_and_isolates_failures() -> None:
    registry = AccountRegistry(
        [
            AccountConfig("first", "first.example.com", 993, "first@example.com"),
            AccountConfig("second", "second.example.com", 993, "second@example.com"),
        ]
    )

    class Credentials:
        async def load_secret(self, account_id):
            if account_id == "second":
                raise KeyError(account_id)
            return "secret"

    class BatchClient:
        def __init__(self, account, secret):
            self.account = account

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def list_mailboxes(self):
            return (Mailbox(f"{self.account.account_id}-INBOX", "/", ()),)

    broker = BrokerService(registry, Credentials(), BatchClient)
    outcomes = asyncio.run(broker.list_mailboxes_batch(("first", "second")))
    assert [(item.account_id, item.ok, item.error.code if item.error else None) for item in outcomes] == [
        ("first", True, None), ("second", False, "not_found")
    ]


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


def test_batch_reads_overlap_across_accounts_without_holding_budget_lock() -> None:
    entered = threading.Barrier(2)

    class Credentials:
        async def load_secret(self, account_id):
            return "secret"

    class ReadClient:
        def __init__(self, account, secret):
            self.account = account

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get_message_budgeted(self, identity, reserve_source):
            assert reserve_source(4)
            entered.wait(timeout=1)
            return MessageContent(identity, {}, "small", (), (), 10)

    registry = AccountRegistry([
        AccountConfig("a", "a.invalid", 993, "a@example.invalid"),
        AccountConfig("b", "b.invalid", 993, "b@example.invalid"),
    ])
    identities = (MessageIdentity("a", "INBOX", "1", "1"), MessageIdentity("b", "INBOX", "1", "2"))
    results = asyncio.run(BrokerService(registry, Credentials(), ReadClient).get_emails(identities))
    assert [item.ok for item in results] == [True, True]


def test_batch_read_budget_settlement_rejects_aggregate_oversubscription(monkeypatch) -> None:
    import readndraft_imap_mcp.broker.reads as reads

    monkeypatch.setattr(reads, "MAX_MESSAGE_BYTES", 10)
    monkeypatch.setattr(reads, "MAX_TEXT_BYTES", 10)

    class ReadClient(FakeClient):
        def get_message(self, identity, max_source_bytes):
            return MessageContent(identity, {}, "123456", (), (), 6)

    broker = BrokerService(
        AccountRegistry([AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]),
        FakeCredentialStore(), ReadClient,
    )
    identities = tuple(MessageIdentity("personal", "INBOX", "1", str(index)) for index in (1, 2))
    results = asyncio.run(broker.get_emails(identities))
    assert [item.ok for item in results] == [True, False]
    assert results[1].error is not None and results[1].error.code == "invalid_request"


def test_two_phase_batch_rejects_overbudget_before_second_body_fetch(monkeypatch) -> None:
    import readndraft_imap_mcp.broker.reads as reads

    monkeypatch.setattr(reads, "MAX_MESSAGE_BYTES", 10)
    fetched: list[str] = []

    class TwoPhaseClient(FakeClient):
        def get_message_budgeted(self, identity, reserve_source):
            if not reserve_source(6):
                raise ValueError("message exceeds the remaining retrieval limit")
            fetched.append(identity.uid)
            return MessageContent(identity, {}, "ok", (), (), 6)

    broker = BrokerService(
        AccountRegistry([AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]),
        FakeCredentialStore(), TwoPhaseClient,
    )
    identities = tuple(MessageIdentity("personal", "INBOX", "1", str(index)) for index in (1, 2))
    results = asyncio.run(broker.get_emails(identities))
    assert [item.ok for item in results] == [True, False]
    assert fetched == ["1"]


def test_two_phase_batch_metadata_failure_releases_the_next_reservation_slot() -> None:
    """A failure before reserve() must not strand another account's read."""
    entered_second = threading.Event()

    class Credentials:
        async def load_secret(self, account_id):
            return "secret"

    class TwoPhaseClient:
        def __init__(self, account, secret):
            self.account = account

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def list_mailboxes(self):
            return (Mailbox("INBOX", "/", ()),)

        def get_message_budgeted(self, identity, reserve_source):
            if identity.uid == "1":
                raise RuntimeError("BODYSTRUCTURE failed before reservation")
            assert reserve_source(4)
            entered_second.set()
            return MessageContent(identity, {}, "ok", (), (), 4)

    registry = AccountRegistry([
        AccountConfig("first", "first.invalid", 993, "first@example.invalid"),
        AccountConfig("second", "second.invalid", 993, "second@example.invalid"),
    ])
    broker = BrokerService(
        registry, Credentials(), TwoPhaseClient, request_timeout_seconds=0.5, max_imap_workers=2
    )
    identities = (
        MessageIdentity("first", "INBOX", "1", "1"),
        MessageIdentity("second", "INBOX", "1", "2"),
    )

    results = asyncio.run(broker.get_emails(identities))

    assert [item.ok for item in results] == [False, True]
    assert entered_second.is_set()
    # Both workers completed; a later call has capacity instead of inheriting
    # a reservation waiter stranded by the failed first item.
    assert asyncio.run(broker.list_mailboxes("first"))[0].name == "INBOX"


def test_two_phase_batch_interleaved_pre_reservation_failure_keeps_request_order() -> None:
    class Credentials:
        async def load_secret(self, account_id):
            return "secret"

    class TwoPhaseClient:
        def __init__(self, account, secret):
            self.account = account

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get_message_budgeted(self, identity, reserve_source):
            if identity.uid == "1":
                raise RuntimeError("metadata failed")
            assert reserve_source(4)
            return MessageContent(identity, {}, identity.uid, (), (), 4)

    registry = AccountRegistry([
        AccountConfig("first", "first.invalid", 993, "first@example.invalid"),
        AccountConfig("second", "second.invalid", 993, "second@example.invalid"),
    ])
    broker = BrokerService(registry, Credentials(), TwoPhaseClient, request_timeout_seconds=0.5, max_imap_workers=2)
    identities = (
        MessageIdentity("first", "INBOX", "1", "1"),
        MessageIdentity("second", "INBOX", "1", "2"),
        MessageIdentity("first", "INBOX", "1", "3"),
    )

    results = asyncio.run(broker.get_emails(identities))

    assert [item.ok for item in results] == [False, True, True]
    assert [item.message.text if item.message else None for item in results] == [None, "2", "3"]


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
    assert [status.status for status in page.target_statuses] == ["complete", "complete"]


def test_interleaved_target_order_consumes_limit_before_later_same_account() -> None:
    from readndraft_imap_mcp.imap.models import SearchResult, SearchWindow

    class SearchClient(FakeClient):
        def __init__(self, account, secret):
            self.account = account

        def search_window(self, mailbox, filters, limit, **kwargs):
            return SearchWindow(
                (SearchResult(MessageIdentity(self.account.account_id, mailbox, "42", "1"), {}, (), 1),),
                "42", None, False,
            )

    registry = AccountRegistry([
        AccountConfig("a", "pinned.example.com", 993, "a@example.com"),
        AccountConfig("b", "pinned.example.com", 993, "b@example.com"),
    ])

    class Credentials:
        async def load_secret(self, account_id):
            return "broker-only-secret"

    page = asyncio.run(BrokerService(registry, Credentials(), SearchClient).search_email_targets(
        (("a", "INBOX"), ("b", "INBOX"), ("a", "Archive")), SearchFilters(), 2
    ))
    assert [(item.identity.account_id, item.identity.mailbox) for item in page.results] == [
        ("a", "INBOX"), ("b", "INBOX")
    ]
    assert page.targets_pending == (SearchTarget("a", "Archive"),)
    assert [status.status for status in page.target_statuses] == ["complete", "complete", "pending"]


def test_multi_target_search_uses_one_shared_deadline() -> None:
    from readndraft_imap_mcp.imap.models import SearchWindow

    class SlowClient(FakeClient):
        def search_window(self, mailbox, filters, limit, **kwargs):
            time.sleep(0.04)
            return SearchWindow((), "42", None, False)

    registry = AccountRegistry([
        AccountConfig("a", "pinned.example.com", 993, "a@example.com"),
        AccountConfig("b", "pinned.example.com", 993, "b@example.com"),
    ])

    class Credentials:
        async def load_secret(self, account_id):
            return "broker-only-secret"

    started = time.monotonic()
    page = asyncio.run(BrokerService(
        registry, Credentials(), SlowClient, request_timeout_seconds=0.05
    ).search_email_targets((("a", "INBOX"), ("b", "INBOX")), SearchFilters(), 2))
    assert time.monotonic() - started < 0.08
    assert page.errors[-1].error.code == "timeout"


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
    assert mixed.errors[0].error.code == "imap_error"
    assert mixed.targets_searched == (
        SearchTarget("personal", "INBOX"),
        SearchTarget("personal", "Broken"),
    )
    assert mixed.targets_pending == ()
    assert [status.status for status in mixed.target_statuses] == ["partial", "error"]
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
    assert page.errors[0].error.code == "imap_error"
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


def test_multi_target_search_shares_one_twenty_request_scan_budget() -> None:
    from readndraft_imap_mcp.imap.models import SearchWindow

    budgets = []

    class SearchClient(FakeClient):
        def search_window(self, mailbox, filters, limit, *, scan_budget=None, **kwargs):
            assert scan_budget is not None
            budgets.append(scan_budget)
            while scan_budget.take_search_request():
                if scan_budget.search_requests % 10 == 0:
                    break
            return SearchWindow((), "42", "1", True, False)

    broker = BrokerService(
        AccountRegistry([AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]),
        FakeCredentialStore(), SearchClient,
    )
    page = asyncio.run(broker.search_email_targets(
        (("personal", "First"), ("personal", "Second")), SearchFilters(), 10
    ))

    assert len(budgets) == 2 and budgets[0] is budgets[1]
    assert budgets[0].search_requests == 20
    assert [status.status for status in page.target_statuses] == ["partial", "partial"]


def test_search_target_statuses_preserve_complete_partial_error_and_pending_order() -> None:
    from readndraft_imap_mcp.imap.client import ImapClientError
    from readndraft_imap_mcp.imap.models import SearchResult, SearchWindow

    class StatusClient(FakeClient):
        def search_window(self, mailbox, filters, limit, **kwargs):
            if mailbox == "broken":
                raise ImapClientError("private detail")
            if mailbox == "partial":
                return SearchWindow((), "42", "9", True, False)
            if mailbox == "fills":
                result = SearchResult(MessageIdentity("personal", mailbox, "42", "8"), {}, (), 1)
                return SearchWindow((result,), "42", None, False)
            return SearchWindow((), "42", None, False)

    broker = BrokerService(
        AccountRegistry([AccountConfig("personal", "pinned.example.com", 993, "leo@example.com")]),
        FakeCredentialStore(), StatusClient,
    )
    targets = (
        ("personal", "complete"),
        ("personal", "partial"),
        ("personal", "broken"),
        ("personal", "fills"),
        ("personal", "pending"),
    )
    page = asyncio.run(broker.search_email_targets(targets, SearchFilters(), 1))

    assert [status.status for status in page.target_statuses] == ["complete", "partial", "error", "complete", "pending"]
    assert page.target_statuses[1].cursor is not None
    assert len(page.errors) == 1
    assert page.errors[0].account_id == "personal"
    assert page.errors[0].mailbox == "broken"
    assert page.errors[0].error.code == "imap_error"
    assert page.targets_searched == tuple(
        SearchTarget("personal", name) for name in ("complete", "partial", "broken", "fills")
    )
    assert page.targets_pending == (SearchTarget("personal", "pending"),)
    assert page.truncated is True
