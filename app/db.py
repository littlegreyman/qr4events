"""数据库引擎与会话。DATABASE_URL 决定 SQLite（本地验证）或 PostgreSQL（生产）。"""
from __future__ import annotations

import os
import sys
from collections.abc import Iterator

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Admin, Base
from .security import hash_password
from .stage import iso_shanghai, now_shanghai


def _build_engine():
    url = settings.database_url
    if url.startswith("sqlite"):
        engine = create_engine(
            url,
            pool_pre_ping=True,
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True, future=True, pool_size=5, max_overflow=10)


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        exists = db.scalar(select(Admin).where(Admin.username == settings.admin_user))
        if exists is None:
            db.add(
                Admin(
                    username=settings.admin_user,
                    password_hash=hash_password(settings.admin_pass),
                    role="admin",
                    created_at=iso_shanghai(now_shanghai()),
                )
            )
            db.commit()
        else:
            # 环境变量中的口令变更时同步哈希，避免改了 .env 却不生效
            from .security import verify_password

            if not verify_password(settings.admin_pass, exists.password_hash):
                exists.password_hash = hash_password(settings.admin_pass)
                db.commit()


def reset_db() -> None:
    """仅供测试：清空并重建所有表。

    加环境守卫，避免生产环境被误调用后直接清空全部业务数据。
    """
    allowed = "pytest" in sys.modules or os.environ.get("ALLOW_DB_RESET") == "1"
    if not allowed:
        raise RuntimeError("reset_db 仅允许在测试环境调用（生产禁止清空数据库）")
    Base.metadata.drop_all(engine)
    init_db()
