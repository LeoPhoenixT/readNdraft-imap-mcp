from __future__ import annotations

import hashlib
import ssl

import certifi
import pytest
from pip._vendor import certifi as pip_certifi


@pytest.mark.parametrize("bundle", [certifi, pip_certifi], ids=["certifi", "pip-vendored-certifi"])
def test_certifi_does_not_trust_compromised_emsign_ecc_g3_root(bundle) -> None:
    """GHSA-6q48-6c7w-9gw2: check trust material, not just package metadata."""
    compromised_root = "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"
    context = ssl.create_default_context(cafile=bundle.where())
    fingerprints = {
        hashlib.sha256(certificate).hexdigest()
        for certificate in context.get_ca_certs(binary_form=True)
    }
    assert fingerprints
    assert compromised_root not in fingerprints
