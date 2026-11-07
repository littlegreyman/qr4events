"""管理员口令哈希、令牌与限流器单测。"""
from __future__ import annotations

import time

from app.security import (
    SlidingWindowLimiter,
    hash_password,
    issue_token,
    verify_password,
    verify_token,
)


def test_password_hash_roundtrip():
    stored = hash_password("S3cret!pass")
    assert "S3cret!pass" not in stored
    assert verify_password("S3cret!pass", stored) is True
    assert verify_password("wrong", stored) is False
    assert verify_password("S3cret!pass", "garbage") is False


def test_token_roundtrip():
    token = issue_token("admin")
    assert verify_token(token) == "admin"


def test_token_tampering_rejected():
    token = issue_token("admin")
    body, sig = token.split(".", 1)
    assert verify_token(f"{body}x.{sig}") is None
    assert verify_token(f"{body}.{sig[:-2]}xx") is None
    assert verify_token("not-a-token") is None


def test_expired_token_rejected(monkeypatch):
    token = issue_token("admin")
    real = time.time
    monkeypatch.setattr("app.security.time.time", lambda: real() + 10 * 3600)
    assert verify_token(token) is None
    monkeypatch.setattr("app.security.time.time", real)


def test_sliding_window_limiter():
    limiter = SlidingWindowLimiter(max_calls=3, window_seconds=10)
    now = 100.0
    assert all(limiter.check("ip1", now + i * 0.1) for i in range(3))
    assert limiter.check("ip1", now + 0.4) is False
    assert limiter.check("ip2", now + 0.4) is True
    assert limiter.check("ip1", now + 11) is True
    limiter.reset()
    assert limiter.check("ip1", now + 12) is True
