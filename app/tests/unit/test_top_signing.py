"""Tests for Taobao TOP MD5 signing protocol.

NOTE (not_checked): the TOP signing canonicalisation (value URL-encoding,
canonical composition) is unverified against an official reference — the
provided official ``api文档`` documents ``sign_method`` values but not the
canonical composition.  The vector tests below are self-consistent and
cannot detect a wrong canonicalisation; replace them with a golden vector
sourced from an authenticated sandbox or a known-good Taobao SDK before any
live use.
"""

from __future__ import annotations

from aidison.providers.taobao import _top_sign


def test_top_sign_is_deterministic() -> None:
    """Same parameters + same secret → same signature every time."""
    params = {
        "method": "taobao.tbk.dg.material.optional.upgrade",
        "app_key": "12345678",
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
        "timestamp": "2026-01-01 00:00:00",
        "q": "sensor",
        "adzone_id": "111111",
        "page_size": "10",
        "page_no": "1",
    }
    secret = "test-secret-abc"
    sig1 = _top_sign(params, secret)
    sig2 = _top_sign(params, secret)
    assert sig1 == sig2
    assert len(sig1) == 32  # MD5 hex
    assert sig1 == sig1.upper()


def test_top_sign_changes_with_secret() -> None:
    params = {
        "method": "taobao.tbk.dg.material.optional.upgrade",
        "app_key": "123",
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
        "timestamp": "2026-01-01 00:00:00",
        "q": "test",
        "adzone_id": "1",
        "page_size": "10",
        "page_no": "1",
    }
    sig_a = _top_sign(params, "secret-a")
    sig_b = _top_sign(params, "secret-b")
    assert sig_a != sig_b


def test_top_sign_changes_with_params() -> None:
    secret = "my-secret"
    base = {
        "method": "taobao.tbk.dg.material.optional.upgrade",
        "app_key": "123",
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
        "timestamp": "2026-01-01 00:00:00",
        "q": "test",
        "adzone_id": "1",
        "page_size": "10",
        "page_no": "1",
    }
    sig1 = _top_sign(base, secret)
    base["q"] = "different"
    sig2 = _top_sign(base, secret)
    assert sig1 != sig2


def test_top_sign_sort_order_matters_not_key_order() -> None:
    """Signature should be the same regardless of insertion order."""
    secret = "s"
    # Keys inserted in non-sorted order
    params_a: dict[str, str] = {}
    params_a["z"] = "last"
    params_a["a"] = "first"
    params_a["m"] = "middle"
    params_b = {"a": "first", "m": "middle", "z": "last"}
    assert _top_sign(params_a, secret) == _top_sign(params_b, secret)


def test_top_sign_self_consistent_vector() -> None:
    """Self-consistency vector computed from the code's own formula.

    not_checked: this is NOT an official TOP golden vector.  It only pins the
    current canonicalisation (secret + sorted(key+value) + secret, MD5
    uppercased) and cannot detect a wrong canonicalisation.
    """
    params = {"k1": "v1", "k2": "v2"}
    secret = "secret"
    # canonical = "k1v1k2v2"
    # payload  = "secret" + "k1v1k2v2" + "secret"
    # MD5("secretk1v1k2v2secret") → compute
    import hashlib

    expected = hashlib.md5(b"secretk1v1k2v2secret").hexdigest().upper()
    assert _top_sign(params, secret) == expected


def test_top_sign_params_with_special_characters() -> None:
    params = {
        "q": "CO2 sensor module ±0.2°C",
        "adzone_id": "111-222",
    }
    secret = "s3cret!"
    sig = _top_sign(params, secret)
    assert len(sig) == 32
    assert sig == sig.upper()
