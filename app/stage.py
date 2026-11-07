"""阶段判定：纯函数，一律使用服务端时间（Asia/Shanghai）。

需求 4.3 六条规则 + 报名结束到报到开始之间的间隙补充。
前端只负责渲染返回的 action，禁止用客户端本地时间做判断。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

# 阶段（按时间窗）
STAGE_NOT_STARTED = "not_started"
STAGE_REGISTRATION = "registration"
STAGE_WAITING = "waiting"
STAGE_CHECKIN = "checkin"
STAGE_ENDED = "ended"

# 用户状态（按手机号 HMAC 查询）
USER_UNKNOWN = "unknown"
USER_NOT_REGISTERED = "not_registered"
USER_REGISTERED = "registered"
USER_CHECKED_IN = "checked_in"

# 前端动作
ACTION_INFO = "info"
ACTION_FORM = "form"
ACTION_COMBO_FORM = "combo_form"
ACTION_BUTTON = "button"
ACTION_MESSAGE = "message"


def now_shanghai() -> datetime:
    """显式取 UTC 再转北京时间，不依赖容器/系统本地时区。"""
    return datetime.now(UTC).astimezone(TZ)


def parse_dt(value: str | datetime | None) -> datetime | None:
    """解析 ISO8601。带偏移按偏移；无偏移按北京时间（后台表单默认语义）。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            text = str(value).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def iso_shanghai(dt: datetime) -> str:
    return dt.astimezone(TZ).isoformat(timespec="seconds")


@dataclass(frozen=True)
class StageResult:
    stage: str
    user_state: str
    action: str
    message: str
    combo: bool = False
    needs_phone: bool = False
    server_time: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "userState": self.user_state,
            "action": self.action,
            "message": self.message,
            "combo": self.combo,
            "needsPhone": self.needs_phone,
            "serverTime": self.server_time,
        }


def determine_stage(
    activity: Any,
    now: datetime | None = None,
    user_state: str = USER_UNKNOWN,
) -> StageResult:
    """按需求 4.3 判定阶段与前端动作。activity 只需具备时间窗与开关属性。"""
    now = (now or now_shanghai()).astimezone(TZ)
    reg_start = parse_dt(getattr(activity, "reg_start", None))
    reg_end = parse_dt(getattr(activity, "reg_end", None))
    checkin_start = parse_dt(getattr(activity, "checkin_start", None))
    checkin_end = parse_dt(getattr(activity, "checkin_end", None))
    reg_enabled = bool(getattr(activity, "reg_enabled", True))
    checkin_enabled = bool(getattr(activity, "checkin_enabled", True))
    combo = bool(getattr(activity, "allow_onsite_combo", True))
    base = {"server_time": iso_shanghai(now), "needs_phone": user_state == USER_UNKNOWN}

    # 时间窗缺失视为配置未完成，安全降级为「未开始」
    if None in (reg_start, reg_end, checkin_start, checkin_end):
        return StageResult(
            STAGE_NOT_STARTED, user_state, ACTION_INFO,
            "活动尚未配置完整时间窗，请稍后再来", **base
        )

    # 6. 当前时间 > 报到结束
    if now > checkin_end:
        return StageResult(
            STAGE_ENDED, user_state, ACTION_INFO, "活动已结束，感谢参与", **base
        )

    # 1. 当前时间 < 报名开始
    if now < reg_start:
        return StageResult(
            STAGE_NOT_STARTED, user_state, ACTION_INFO,
            "报名未开始，请在报名开放后扫码", **base
        )

    # 4/5. 报到期内（优先级高于报名期，避免时间窗重叠时误判）
    if checkin_start <= now <= checkin_end:
        if not checkin_enabled:
            return StageResult(
                STAGE_CHECKIN, user_state, ACTION_MESSAGE,
                "报到通道已由组织者暂时关闭，请联系现场工作人员", **base
            )
        if user_state == USER_CHECKED_IN:
            return StageResult(
                STAGE_CHECKIN, user_state, ACTION_INFO,
                "您已完成报到，无需重复操作", **base
            )
        if user_state == USER_REGISTERED:
            return StageResult(
                STAGE_CHECKIN, user_state, ACTION_BUTTON,
                "您已报名成功，点击下方按钮完成现场报到", **base
            )
        if combo:
            return StageResult(
                STAGE_CHECKIN, user_state, ACTION_COMBO_FORM,
                "现场报名 + 报到：填写信息后一次完成", combo=True, **base
            )
        return StageResult(
            STAGE_CHECKIN, user_state, ACTION_MESSAGE,
            "本活动未开放现场报名，请先完成报名后再扫码报到", **base
        )

    # 2/3. 报名期内
    if reg_start <= now <= reg_end:
        if not reg_enabled:
            return StageResult(
                STAGE_REGISTRATION, user_state, ACTION_MESSAGE,
                "报名通道已由组织者暂时关闭", **base
            )
        if user_state == USER_CHECKED_IN:
            return StageResult(
                STAGE_REGISTRATION, user_state, ACTION_INFO,
                "您已完成报名与报到，感谢参与", **base
            )
        if user_state == USER_REGISTERED:
            return StageResult(
                STAGE_REGISTRATION, user_state, ACTION_MESSAGE,
                "您已报名成功，请在活动开始后扫码报到", **base
            )
        return StageResult(
            STAGE_REGISTRATION, user_state, ACTION_FORM,
            "请填写以下信息完成报名", **base
        )

    # 补充：报名已结束、报到未开始的间隙（需求 4.3 未单列，但必然存在）
    if user_state == USER_REGISTERED or user_state == USER_CHECKED_IN:
        return StageResult(
            STAGE_WAITING, user_state, ACTION_MESSAGE,
            "报名已截止，请在活动开始后扫码报到", **base
        )
    return StageResult(
        STAGE_WAITING, user_state, ACTION_MESSAGE,
        "报名已截止，如有疑问请联系组织者", **base
    )
