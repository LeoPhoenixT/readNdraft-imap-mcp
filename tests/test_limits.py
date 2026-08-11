from __future__ import annotations

import pytest

from readndraft_imap_mcp.broker import AccountRequestQuota, RequestQuotaError


def test_rate_quota_is_per_account_and_sliding() -> None:
    now = [100.0]
    quota = AccountRequestQuota(requests_per_minute=2, clock=lambda: now[0])

    with quota.slot("personal"):
        pass
    with quota.slot("personal"):
        pass
    with pytest.raises(RequestQuotaError, match="rate"):
        with quota.slot("personal"):
            pass
    with quota.slot("work"):
        pass

    now[0] += 61
    with quota.slot("personal"):
        pass


def test_concurrency_quota_releases_after_failure() -> None:
    quota = AccountRequestQuota(max_concurrent=1)

    with quota.slot("personal"):
        with pytest.raises(RequestQuotaError, match="concurrency"):
            with quota.slot("personal"):
                pass

    with quota.slot("personal"):
        pass


def test_batch_quota_charges_each_item_atomically() -> None:
    now = [100.0]
    quota = AccountRequestQuota(requests_per_minute=3, clock=lambda: now[0])

    with quota.slot("personal", cost=2):
        pass
    with pytest.raises(RequestQuotaError, match="rate"):
        with quota.slot("personal", cost=2):
            pass
    with quota.slot("personal"):
        pass


def test_batch_quota_rejects_invalid_cost() -> None:
    quota = AccountRequestQuota()
    with pytest.raises(ValueError, match="cost"):
        with quota.slot("personal", cost=0):
            pass
