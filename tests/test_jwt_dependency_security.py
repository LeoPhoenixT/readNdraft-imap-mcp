from __future__ import annotations

import jwt
import pytest


@pytest.mark.parametrize("decode", [jwt.decode, jwt.decode_complete], ids=["decode", "decode_complete"])
def test_reused_jwt_options_do_not_disable_expiration_checks(decode):
    """Guard the MCP JWT dependency against CVE-2026-103001."""
    key = b"synthetic-jwt-regression-key-00000"
    token = jwt.encode({"exp": 0}, key, algorithm="HS256")
    options = {"verify_signature": False}

    decode(token, options=options)
    assert options == {"verify_signature": False}

    options["verify_signature"] = True
    with pytest.raises(jwt.ExpiredSignatureError):
        decode(token, key, algorithms=["HS256"], options=options)
