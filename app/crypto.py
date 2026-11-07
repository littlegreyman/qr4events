"""手机号加密存储与索引。

- 明文 AES-GCM 加密后存 phone_enc；
- HMAC-SHA256(规范化手机号) 存 phone_hash，用于唯一约束与精确搜索
  （HMAC 是确定性的，密文不是，所以唯一约束只能建在哈希上）；
- 展示层统一脱敏；
- 密钥由主密钥派生，见 config.derive。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import derive

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
_AAD = b"qr4events.phone.v1"


def normalize_phone(raw: str) -> str:
    """规范化并校验手机号；任何非法输入抛 ValueError，由上层转成 400。"""
    if not isinstance(raw, str):
        raise ValueError("手机号格式不正确")
    s = re.sub(r"[\s\-()]", "", raw.strip())
    if s.startswith("+86"):
        s = s[3:]
    elif s.startswith("86") and len(s) == 13:
        s = s[2:]
    if not _PHONE_RE.match(s):
        raise ValueError("手机号格式不正确（需 11 位中国大陆手机号）")
    return s


class PhoneCipher:
    def __init__(self) -> None:
        self._aead = AESGCM(derive("qr4events/phone-aes-v1"))
        self._hmac_key = derive("qr4events/phone-hmac-v1")

    def encrypt(self, phone: str) -> str:
        nonce = secrets.token_bytes(12)
        ciphertext = self._aead.encrypt(nonce, phone.encode("utf-8"), _AAD)
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            raw = base64.urlsafe_b64decode(token.encode("ascii"))
            return self._aead.decrypt(raw[:12], raw[12:], _AAD).decode("utf-8")
        except Exception as exc:
            raise ValueError("手机号密文无法解密（密钥可能已变更）") from exc

    def digest(self, phone: str) -> str:
        return hmac.new(self._hmac_key, phone.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def mask(phone: str) -> str:
        if len(phone) >= 7:
            return phone[:3] + "****" + phone[-4:]
        return "***"


cipher = PhoneCipher()
