"""手机号加密、HMAC 索引、脱敏与输入规范化单测。"""
from __future__ import annotations

import pytest

from app.crypto import cipher, normalize_phone


@pytest.mark.parametrize(
    "raw",
    ["13800138000", "138 0013 8000", "138-0013-8000", "+8613800138000", "(138)00138000"],
)
def test_normalize(raw):
    assert normalize_phone(raw) == "13800138000"


@pytest.mark.parametrize(
    "raw", ["", "abc", "12345", "12800138000", "1380013800a", None, 13800138000]
)
def test_reject_invalid(raw):
    with pytest.raises(ValueError):
        normalize_phone(raw)


def test_encrypt_decrypt_roundtrip():
    token = cipher.encrypt("13800138000")
    assert cipher.decrypt(token) == "13800138000"
    assert "13800138000" not in token


def test_encryption_is_randomized():
    a = cipher.encrypt("13800138000")
    b = cipher.encrypt("13800138000")
    assert a != b
    assert cipher.decrypt(a) == cipher.decrypt(b) == "13800138000"


def test_digest_is_deterministic_and_distinct():
    d1 = cipher.digest("13800138000")
    d2 = cipher.digest("13800138000")
    d3 = cipher.digest("13900139000")
    assert d1 == d2 and len(d1) == 64
    assert d1 != d3


def test_mask():
    assert cipher.mask("13800138000") == "138****8000"


def test_tampered_ciphertext_rejected():
    token = cipher.encrypt("13800138000")
    chars = list(token)
    chars[20] = "A" if chars[20] != "A" else "B"
    with pytest.raises(ValueError):
        cipher.decrypt("".join(chars))
