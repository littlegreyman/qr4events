"""Pydantic 请求模型（契约对齐需求文档第 8 节）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class StatusRequest(BaseModel):
    phone: str | None = None


class RegisterRequest(BaseModel):
    phone: str
    name: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=50)
    department: str = Field(default="", max_length=200)
    unit: str = Field(default="", max_length=200)
    remark: str = Field(default="", max_length=2000)
    invite_code: str = Field(default="", max_length=64)
    source: str = Field(default="qr", max_length=30)

    @field_validator("name", "category", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v


class CheckinRequest(BaseModel):
    phone: str
    name: str = Field(default="", max_length=100)
    category: str = Field(default="", max_length=50)
    department: str = Field(default="", max_length=200)
    unit: str = Field(default="", max_length=200)
    remark: str = Field(default="", max_length=2000)
    invite_code: str = Field(default="", max_length=64)
    source: str = Field(default="qr", max_length=30)


class FieldToggle(BaseModel):
    enabled: bool = True
    required: bool = False


class FieldsConfig(BaseModel):
    type_options: list[str] = Field(default_factory=lambda: ["普通", "VIP", "其他"])
    department: FieldToggle = Field(default_factory=FieldToggle)
    unit: FieldToggle = Field(default_factory=FieldToggle)
    remark: FieldToggle = Field(default_factory=FieldToggle)

    @field_validator("type_options")
    @classmethod
    def _options(cls, v: list[str]) -> list[str]:
        cleaned = [str(x).strip() for x in v if str(x).strip()]
        if not cleaned:
            raise ValueError("类型选项至少需要一项")
        if len(cleaned) > 20:
            raise ValueError("类型选项最多 20 项")
        return cleaned


class ActivityWrite(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    location: str = Field(default="", max_length=200)
    reg_start: str = ""
    reg_end: str = ""
    checkin_start: str = ""
    checkin_end: str = ""
    status: str = Field(default="active", pattern="^(draft|active|ended)$")
    reg_mode: str = Field(default="open", pattern="^(open|whitelist|invite)$")
    invite_code: str = Field(default="", max_length=64)
    fields_config: FieldsConfig = Field(default_factory=FieldsConfig)
    allow_onsite_combo: bool = True
    reg_enabled: bool = True
    checkin_enabled: bool = True

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v


class LoginRequest(BaseModel):
    username: str = Field(max_length=80)
    password: str = Field(min_length=1, max_length=1024)


class WhitelistItem(BaseModel):
    name: str = Field(default="", max_length=100)
    phone: str


class WhitelistWrite(BaseModel):
    items: list[WhitelistItem] = Field(default_factory=list, max_length=10_000)


class QrcodeQuery(BaseModel):
    size: int = Field(default=10, ge=4, le=40)
    format: str = Field(default="png", pattern="^(png|svg)$")
