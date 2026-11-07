"""SQLAlchemy 2.x ORM 模型。

时间统一存 ISO8601 带偏移字符串（而非 DB DateTime），避免 SQLite 无时区
与 PostgreSQL 时区语义差异导致的时间窗偏移。
"""
from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import DEFAULT_FIELDS


class Base(DeclarativeBase):
    pass


class Activity(Base):
    __tablename__ = "activities"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    location: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    reg_start: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    reg_end: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    checkin_start: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    checkin_end: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    reg_mode: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    invite_code: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    fields_config: Mapped[dict] = mapped_column(
        JSON, default=lambda: dict(DEFAULT_FIELDS), nullable=False
    )
    allow_onsite_combo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reg_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    checkin_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), default="", nullable=False)


class Registration(Base):
    __tablename__ = "registrations"
    __table_args__ = (
        UniqueConstraint("activity_id", "phone_hash", name="uq_registration_activity_phone"),
        Index("ix_registration_activity_time", "activity_id", "created_at"),
        Index("ix_registration_phone", "phone_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    activity_id: Mapped[str] = mapped_column(
        String(16), ForeignKey("activities.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone_enc: Mapped[str] = mapped_column(Text, nullable=False)
    phone_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    department: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    unit: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    remark: Mapped[str] = mapped_column(Text, default="", nullable=False)
    extra: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    source: Mapped[str] = mapped_column(String(30), default="qr", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), default="", nullable=False)


class CheckIn(Base):
    __tablename__ = "checkins"
    __table_args__ = (
        UniqueConstraint("activity_id", "phone_hash", name="uq_checkin_activity_phone"),
        UniqueConstraint("registration_id", name="uq_checkin_registration"),
        Index("ix_checkin_activity_time", "activity_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    activity_id: Mapped[str] = mapped_column(
        String(16), ForeignKey("activities.id", ondelete="CASCADE"), nullable=False
    )
    registration_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("registrations.id", ondelete="CASCADE"), nullable=False
    )
    phone_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(30), default="qr", nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), default="", nullable=False)


class Whitelist(Base):
    __tablename__ = "whitelists"
    __table_args__ = (
        UniqueConstraint("activity_id", "phone_hash", name="uq_whitelist_activity_phone"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    activity_id: Mapped[str] = mapped_column(
        String(16), ForeignKey("activities.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    phone_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), default="", nullable=False)


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="admin", nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), default="", nullable=False)
