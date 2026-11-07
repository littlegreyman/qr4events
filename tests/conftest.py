"""pytest 全局环境：在导入 app 之前固定密钥与独立测试库。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

TEST_DB = Path(tempfile.gettempdir()) / "qr4events_test.db"
for suffix in ("", "-wal", "-shm", "-journal"):
    p = Path(str(TEST_DB) + suffix)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"
os.environ["SECRET_KEY"] = "qr4events-pytest-fixed-master-key-v1"
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASS"] = "TestAdmin!2026"
os.environ["PUBLIC_BASE_URL"] = "https://qr.example.com"
os.environ["APP_TZ"] = "Asia/Shanghai"
os.environ["SERVE_STATIC"] = "1"
os.environ["RATE_LIMIT_GENERAL"] = "1000"
os.environ["RATE_LIMIT_SUBMIT"] = "1000"


@pytest.fixture(autouse=True)
def clean_db():
    from app.db import reset_db
    from app.security import general_limiter, login_limiter, submit_limiter

    reset_db()
    general_limiter.reset()
    submit_limiter.reset()
    login_limiter.reset()
    yield


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth(client):
    r = client.post(
        "/api/admin/login",
        json={"username": "admin", "password": "TestAdmin!2026"},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.fixture()
def activity_payload():
    """默认：当前处于报名期。按小时偏移构造时间窗。"""
    from datetime import timedelta

    from app.stage import iso_shanghai, now_shanghai

    def build(
        name: str = "测试活动",
        reg_start: float = -1,
        reg_end: float = 1,
        checkin_start: float = 2,
        checkin_end: float = 4,
        **kwargs,
    ) -> dict:
        now = now_shanghai()

        def point(value):
            # 数值 = 相对当前时刻的小时偏移；字符串 = 直接使用的 ISO/非法值
            return value if isinstance(value, str) else iso_shanghai(now + timedelta(hours=value))

        base = {
            "name": name,
            "description": "用于自动化测试",
            "location": "测试会场",
            "reg_start": point(reg_start),
            "reg_end": point(reg_end),
            "checkin_start": point(checkin_start),
            "checkin_end": point(checkin_end),
            "status": "active",
            "reg_mode": "open",
            "invite_code": "",
            "fields_config": {
                "type_options": ["普通", "VIP", "其他"],
                "department": {"enabled": True, "required": False},
                "unit": {"enabled": True, "required": False},
                "remark": {"enabled": False, "required": False},
            },
            "allow_onsite_combo": True,
            "reg_enabled": True,
            "checkin_enabled": True,
        }
        base.update(kwargs)
        return base

    return build


@pytest.fixture()
def create_activity(client, auth, activity_payload):
    def _create(**kwargs):
        r = client.post(
            "/api/admin/activities", json=activity_payload(**kwargs), headers=auth
        )
        assert r.status_code == 200, r.text
        return r.json()["activity"]

    return _create
