from __future__ import annotations

import asyncio

import pytest

from readndraft_imap_mcp.broker import AccountConfig, AccountRegistry, BrokerService
from readndraft_imap_mcp.broker.limits import AccountRequestQuota, RequestQuotaError
from readndraft_imap_mcp.imap.models import (
    FlagChange,
    Mailbox,
    MessageContent,
    MessageIdentity,
    SearchFilters,
    SearchWindow,
)


class Credentials:
    def __init__(self):
        self.loaded = []

    async def load_secret(self, account_id):
        self.loaded.append(account_id)
        return "synthetic-test-value"


class Audit:
    async def record(self, event):
        pass


class Client:
    def __init__(self, account, secret):
        self.account = account

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def list_mailboxes(self):
        return (Mailbox("INBOX", "/", ()),)

    def search_window(self, *args, **kwargs):
        return SearchWindow((), "1", None, False)

    def get_message_budgeted(self, identity, reserve_source):
        assert reserve_source(2)
        return MessageContent(identity, {}, "ok", (), (), 2)

    def set_star(self, identity, enabled):
        return FlagChange(identity, "starred", enabled, True, (), (r"\Flagged",))

    def set_read_state(self, identity, enabled):
        return FlagChange(identity, "read", enabled, True, (), (r"\Seen",))


class CountingQuota(AccountRequestQuota):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.admissions = []

    def admit_task(self, keys):
        self.admissions.append(keys)
        return super().admit_task(keys)


def build_broker(quota=None):
    credentials = Credentials()
    registry = AccountRegistry([
        AccountConfig("valid", "imap.invalid", 993, "same@example.invalid"),
        AccountConfig("alias", "IMAP.invalid.", 993, "same@example.invalid"),
        AccountConfig("other", "other.invalid", 993, "other@example.invalid"),
        AccountConfig("disabled", "disabled.invalid", 993, "other@example.invalid", enabled=False),
    ])
    return BrokerService(registry, credentials, Client, audit=Audit(), quota=quota), credentials


async def invoke(broker, operation, account_ids):
    if operation == "mailboxes":
        return await broker.list_mailboxes_batch(account_ids)
    if operation == "search":
        page = await broker.search_email_targets(tuple((name, "INBOX") for name in account_ids), SearchFilters())
        return page.target_statuses
    ids = tuple(MessageIdentity(name, "INBOX", "1", str(i + 1)) for i, name in enumerate(account_ids))
    if operation == "reads":
        return await broker.get_emails(ids)
    if operation == "stars":
        return await broker.set_star_batch(ids, True)
    return await broker.set_read_state_batch(ids, True)


@pytest.mark.parametrize("operation", ["mailboxes", "search", "reads", "stars", "read_state"])
@pytest.mark.parametrize("invalid,code", [("missing", "not_found"), ("disabled", "permission_denied")])
@pytest.mark.parametrize("invalid_first", [False, True])
def test_invalid_account_does_not_poison_valid_account(operation, invalid, code, invalid_first):
    quota = CountingQuota()
    broker, credentials = build_broker(quota)
    names = (invalid, "valid") if invalid_first else ("valid", invalid)
    results = asyncio.run(invoke(broker, operation, names))
    valid_index = names.index("valid")
    assert results[valid_index].error is None
    assert results[1 - valid_index].error.code == code
    assert credentials.loaded == ["valid"]
    assert quota.admissions == [(('imap.invalid', 993, 'same@example.invalid'),)]
    assert quota.usage()["active_sessions"] == 0


@pytest.mark.parametrize("operation", ["mailboxes", "search", "reads", "stars", "read_state"])
def test_all_invalid_accounts_do_not_consume_quota_or_load_credentials(operation):
    quota = CountingQuota()
    broker, credentials = build_broker(quota)
    results = asyncio.run(invoke(broker, operation, ("missing", "disabled")))
    assert [item.error.code for item in results] == ["not_found", "permission_denied"]
    assert credentials.loaded == []
    assert quota.admissions == []


def test_aliases_in_one_batch_are_admitted_once_per_physical_account():
    quota = CountingQuota(requests_per_minute=1, refill_per_second=0.000001)
    broker, credentials = build_broker(quota)
    results = asyncio.run(broker.list_mailboxes_batch(("valid", "missing", "alias")))
    assert [item.ok for item in results] == [True, False, True]
    assert len(quota.admissions) == 1 and len(quota.admissions[0]) == 1
    assert sorted(credentials.loaded) == ["alias", "valid"]
    with pytest.raises(RequestQuotaError):
        asyncio.run(broker._client_call("valid", lambda client: None))


def test_rejected_atomic_admission_is_not_retried_or_charged_partially():
    quota = CountingQuota(requests_per_minute=1, refill_per_second=0.000001)
    broker, credentials = build_broker(quota)
    key = ("imap.invalid", 993, "same@example.invalid")
    quota.admit_task((key,))
    quota.admissions.clear()
    results = asyncio.run(broker.list_mailboxes_batch(("valid", "other", "missing")))
    assert [item.error.code for item in results] == ["rate_limited", "rate_limited", "not_found"]
    assert len(quota.admissions) == 1
    assert credentials.loaded == []
    assert asyncio.run(broker._client_call("other", lambda client: "available")) == "available"
    assert credentials.loaded == ["other"]
