"""管理员鉴权、登录令牌与接口限流。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import threading
import time
from collections import OrderedDict, deque

from fastapi import Header, HTTPException, Request, status

from .config import derive, settings

_PBKDF2_ITERATIONS = 200_000
_MIN_ACCEPTED_PBKDF2_ITERATIONS = 100_000
_MAX_ACCEPTED_PBKDF2_ITERATIONS = 1_000_000


def hash_password(password: str) -> str:
    salt = derive("qr4events/admin-salt-v1")
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${base64.b64encode(dk).decode('ascii')}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, b64 = stored.split("$", 2)
        rounds = int(iterations)
        if (
            algo != "pbkdf2_sha256"
            or not _MIN_ACCEPTED_PBKDF2_ITERATIONS
            <= rounds
            <= _MAX_ACCEPTED_PBKDF2_ITERATIONS
        ):
            return False
        salt = derive("qr4events/admin-salt-v1")
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, rounds
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
        if not isinstance(payload, dict):
            return None
        username = payload.get("u")
        expires = payload.get("exp")
        if (
            not isinstance(username, str)
            or not username
            or type(expires) is not int
        ):
            return None
        if expires < int(time.time()):
            return None
        return username
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
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_keys = 10_000

    def check(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                if len(self._hits) >= self._max_keys:
                    self._hits.popitem(last=False)
                bucket = deque()
                self._hits[key] = bucket
            else:
                self._hits.move_to_end(key)
            cutoff = now - self.window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if not bucket:
                del self._hits[key]
                bucket = deque()
                self._hits[key] = bucket
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
    只有在直连对端 IP 属于 TRUSTED_PROXY_IPS 且 TRUST_PROXY=1 时才解析。
    边缘代理必须覆盖或安全追加 X-Forwarded-For。
    """
    peer = request.client.host if request.client else "unknown"
    if settings.trust_proxy and settings.trusted_proxy_ips:
        try:
            peer_ip = ipaddress.ip_address(peer)
            trusted = any(peer_ip in network for network in settings.trusted_proxy_ips)
        except ValueError:
            trusted = False
        if not trusted:
            return peer
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            try:
                # The trusted edge proxy must overwrite/append the actual client IP.
                return str(ipaddress.ip_address(parts[-1]))
            except ValueError:
                pass
    return peer
