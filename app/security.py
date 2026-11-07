"""管理员鉴权、登录令牌与接口限流。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException, Request, status

from .config import derive, settings

_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = derive("qr4events/admin-salt-v1")
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${base64.b64encode(dk).decode('ascii')}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, b64 = stored.split("$", 2)
        if algo != "pbkdf2_sha256":
            return False
        salt = derive("qr4events/admin-salt-v1")
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(base64.b64encode(dk).decode("ascii"), b64)
    except (ValueError, TypeError):
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(username: str) -> str:
    payload = {
        "u": username,
        "exp": int(time.time()) + settings.token_ttl,
        "n": secrets.token_hex(8),
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(derive("qr4events/token-v1"), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token: str) -> str | None:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(
            hmac.new(derive("qr4events/token-v1"), body.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_unb64(body))
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload.get("u")
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def require_admin(
    authorization: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None),
) -> str:
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_admin_token:
        token = x_admin_token.strip()
    username = verify_token(token) if token else None
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录已过期，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return username


class SlidingWindowLimiter:
    """线程安全的滑动窗口限流。进程内实现，单实例/单容器足够；
    多副本生产环境应换成共享存储（Redis），接口保持不变。"""

    def __init__(self, max_calls: int, window_seconds: int) -> None:
        self.max_calls = max_calls
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._hits[key]
            cutoff = now - self.window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_calls:
                return False
            bucket.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


general_limiter = SlidingWindowLimiter(settings.rate_limit_general, settings.rate_window)
submit_limiter = SlidingWindowLimiter(settings.rate_limit_submit, settings.rate_window)
login_limiter = SlidingWindowLimiter(settings.rate_limit_login, settings.rate_window)


def client_ip(request: Request) -> str:
    """默认只信任传输层对端 IP。

    X-Forwarded-For 是客户端可伪造的请求头，若无条件取最左值，
    攻击者每次换一个假 IP 就能让限流 key 永远不同，限流形同虚设。
    只有在明确配置了可信反向代理（TRUST_PROXY=1）时才解析，
    且取最右侧一跳 —— 那是离可信代理最近、不可由客户端任意追加的地址。
    """
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"
