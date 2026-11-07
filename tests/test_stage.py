"""需求 4.3 六条阶段判定规则 · 纯函数单测（含边界、时区、手动开关）。"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.stage import (
    ACTION_BUTTON,
    ACTION_COMBO_FORM,
    ACTION_FORM,
    ACTION_INFO,
    ACTION_MESSAGE,
    STAGE_CHECKIN,
    STAGE_ENDED,
    STAGE_NOT_STARTED,
    STAGE_REGISTRATION,
    STAGE_WAITING,
    USER_CHECKED_IN,
    USER_NOT_REGISTERED,
    USER_REGISTERED,
    USER_UNKNOWN,
    determine_stage,
    parse_dt,
)

TZ = ZoneInfo("Asia/Shanghai")
DAY = datetime(2026, 11, 20, tzinfo=TZ)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


def act(**kwargs):
    base = {
        "reg_start": "2026-11-20T09:00:00+08:00",
        "reg_end": "2026-11-20T12:00:00+08:00",
        "checkin_start": "2026-11-20T13:00:00+08:00",
        "checkin_end": "2026-11-20T17:00:00+08:00",
        "reg_enabled": True,
        "checkin_enabled": True,
        "allow_onsite_combo": True,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_rule1_before_registration():
    r = determine_stage(act(), at(8, 0), USER_NOT_REGISTERED)
    assert r.stage == STAGE_NOT_STARTED
    assert r.action == ACTION_INFO
    assert "未开始" in r.message


def test_rule2_registration_form():
    r = determine_stage(act(), at(10, 0), USER_NOT_REGISTERED)
    assert r.stage == STAGE_REGISTRATION
    assert r.action == ACTION_FORM


def test_rule3_already_registered():
    r = determine_stage(act(), at(10, 0), USER_REGISTERED)
    assert r.stage == STAGE_REGISTRATION
    assert r.action == ACTION_MESSAGE
    assert "已报名" in r.message


def test_rule4_checkin_button():
    r = determine_stage(act(), at(14, 0), USER_REGISTERED)
    assert r.stage == STAGE_CHECKIN
    assert r.action == ACTION_BUTTON


def test_rule5_combo_form_and_disabled():
    r = determine_stage(act(), at(14, 0), USER_NOT_REGISTERED)
    assert r.stage == STAGE_CHECKIN
    assert r.action == ACTION_COMBO_FORM and r.combo is True
    r2 = determine_stage(act(allow_onsite_combo=False), at(14, 0), USER_NOT_REGISTERED)
    assert r2.action == ACTION_MESSAGE and "先完成报名" in r2.message


def test_rule6_ended():
    r = determine_stage(act(), at(18, 0), USER_NOT_REGISTERED)
    assert r.stage == STAGE_ENDED
    assert r.action == ACTION_INFO


def test_gap_between_reg_end_and_checkin_start():
    r = determine_stage(act(), at(12, 30), USER_REGISTERED)
    assert r.stage == STAGE_WAITING
    assert r.action == ACTION_MESSAGE


def test_boundaries_are_inclusive():
    assert determine_stage(act(), at(9, 0), USER_UNKNOWN).stage == STAGE_REGISTRATION
    assert determine_stage(act(), at(12, 0), USER_UNKNOWN).stage == STAGE_REGISTRATION
    assert determine_stage(act(), at(13, 0), USER_UNKNOWN).stage == STAGE_CHECKIN
    assert determine_stage(act(), at(17, 0), USER_UNKNOWN).stage == STAGE_CHECKIN
    assert determine_stage(act(), at(17, 0) + timedelta(seconds=1), USER_UNKNOWN).stage == STAGE_ENDED


def test_manual_switches():
    r = determine_stage(act(reg_enabled=False), at(10, 0), USER_NOT_REGISTERED)
    assert r.action == ACTION_MESSAGE and "关闭" in r.message
    r2 = determine_stage(act(checkin_enabled=False), at(14, 0), USER_REGISTERED)
    assert r2.action == ACTION_MESSAGE


def test_checked_in_state_is_terminal():
    r = determine_stage(act(), at(14, 0), USER_CHECKED_IN)
    assert r.action == ACTION_INFO and "已完成报到" in r.message


def test_unknown_user_needs_phone():
    r = determine_stage(act(), at(10, 0), USER_UNKNOWN)
    assert r.needs_phone is True


def test_naive_datetime_treated_as_shanghai():
    dt = parse_dt("2026-11-20T09:00:00")
    assert dt is not None
    assert dt.utcoffset().total_seconds() == 8 * 3600


def test_utc_offset_normalized_to_shanghai():
    """UTC 01:00 == 北京 09:00，必须判为报名期开始而非未开始。"""
    a = act(reg_start="2026-11-20T01:00:00+00:00")
    r = determine_stage(a, at(9, 0), USER_NOT_REGISTERED)
    assert r.stage == STAGE_REGISTRATION


def test_missing_window_fails_closed():
    r = determine_stage(act(reg_end=""), at(10, 0), USER_NOT_REGISTERED)
    assert r.stage == STAGE_NOT_STARTED


def test_server_time_is_shanghai_iso():
    r = determine_stage(act(), at(10, 0), USER_NOT_REGISTERED)
    assert "+08:00" in r.server_time
