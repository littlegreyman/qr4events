"""集中配置：全部来自环境变量，密钥不入库。

设计要点
- DATABASE_URL 切换本地 SQLite（验证）与生产 PostgreSQL，业务代码不变。
- 主密钥优先读 SECRET_KEY；未设置时在本地 data/ 下生成并持久化，
  保证服务重启后仍能解密历史手机号（否则加密数据会永久丢失）。
- 管理员口令优先读 ADMIN_PASS；未设置时生成随机口令写入 data/ 并打印一次，
  绝不硬编码默认口令。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

DEFAULT_FIELDS: dict = {
    "type_options": ["普通", "VIP", "其他"],
    "department": {"enabled": True, "required": False},
    "unit": {"enabled": True, "required": False},
    "remark": {"enabled": False, "required": False},
}


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def _master_key() -> bytes:
    raw = os.environ.get("SECRET_KEY")
    if raw:
        return hashlib.sha256(raw.encode("utf-8")).digest()
    path = ensure_data_dir() / "secret.key"
    if path.exists():
        key = path.read_bytes()
        if len(key) >= 32:
            return key
    key = secrets.token_bytes(32)
    path.write_bytes(key)
    os.chmod(path, 0o600)
    return key


MASTER_KEY = _master_key()


def derive(label: str) -> bytes:
    """从主密钥派生互相独立的子密钥，避免密钥复用。"""
    return hmac.new(MASTER_KEY, label.encode("utf-8"), hashlib.sha256).digest()


def _admin_password() -> tuple[str, bool]:
    pw = os.environ.get("ADMIN_PASS")
    if pw:
        return pw, False
    path = ensure_data_dir() / "initial_admin_password.txt"
    if path.exists() and path.read_text().strip():
        return path.read_text().strip(), False
    pw = "Adm!" + secrets.token_urlsafe(14)
    path.write_text(pw)
    os.chmod(path, 0o600)
    return pw, True


# 只解析一次，避免 dataclass 两个字段各生成/读取一次口令
_ADMIN_CREDENTIAL = _admin_password()


def _bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get(
        "DATABASE_URL", f"sqlite:///{ensure_data_dir() / 'app.db'}"
    )
    tz: str = os.environ.get("APP_TZ", "Asia/Shanghai")
    public_base_url: str = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    admin_user: str = os.environ.get("ADMIN_USER", "admin")
    admin_pass: str = _ADMIN_CREDENTIAL[0]
    admin_pass_generated: bool = _ADMIN_CREDENTIAL[1]
    token_ttl: int = _int("TOKEN_TTL", 8 * 3600)
    serve_static: bool = _bool("SERVE_STATIC", True)
    trust_proxy: bool = _bool("TRUST_PROXY", False)
    rate_limit_general: int = _int("RATE_LIMIT_GENERAL", 180)
    rate_limit_submit: int = _int("RATE_LIMIT_SUBMIT", 30)
    rate_limit_login: int = _int("RATE_LIMIT_LOGIN", 10)
    rate_window: int = _int("RATE_WINDOW", 60)
    short_id_len: int = _int("SHORT_ID_LEN", 6)
    qr_box_size_default: int = 10
    combo_default: bool = True
    default_fields: dict = field(default_factory=lambda: dict(DEFAULT_FIELDS))
    cors_origins: str = os.environ.get("CORS_ORIGINS", "")


settings = Settings()

# 时区必须固定，否则报名/签到时间窗整体偏移 8 小时。
if settings.tz != "Asia/Shanghai":
    import warnings

    warnings.warn(
        f"APP_TZ={settings.tz} 与需求不符；阶段判定强制使用 Asia/Shanghai",
        RuntimeWarning,
        stacklevel=2,
    )
