"""业务服务层：所有写入与查询的唯一入口。

事务与幂等
- 报名/签到均幂等：重复提交返回 already_* 状态，不重复计数；
- 2合1 为单事务写 registration + checkin 两张表；
- 唯一约束兜底 IntegrityError，防止并发下重复写入。
"""
from __future__ import annotations

import csv
import io
import secrets
import threading
from typing import Any

import qrcode
from qrcode.constants import ERROR_CORRECT_H
from qrcode.image.svg import SvgPathImage
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .crypto import cipher, normalize_phone
from .models import Activity, CheckIn, Registration, Whitelist
from .schemas import ActivityWrite, CheckinRequest, RegisterRequest, WhitelistWrite
from .stage import (
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
    iso_shanghai,
    now_shanghai,
    parse_dt,
)

_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
_FIELD_LABELS = {"department": "所属部门", "unit": "单位", "remark": "备注"}

# 写事务串行化：SQLite 高并发写会抛 "database is locked"，且可消除
# 「先查后写」的竞态。多副本生产由数据库唯一约束 + IntegrityError 兜底。
_WRITE_LOCK = threading.RLock()


class ServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _now_iso() -> str:
    return iso_shanghai(now_shanghai())


def generate_short_id(db: Session) -> str:
    for _ in range(30):
        sid = "".join(secrets.choice(_ALPHABET) for _ in range(settings.short_id_len))
        if db.get(Activity, sid) is None:
            return sid
    raise ServiceError("无法生成唯一活动 ID，请重试", 500)


def activity_public_info(a: Activity) -> dict[str, Any]:
    return {
        "id": a.id,
        "name": a.name,
        "description": a.description,
        "location": a.location,
        "regStart": a.reg_start,
        "regEnd": a.reg_end,
        "checkInStart": a.checkin_start,
        "checkInEnd": a.checkin_end,
        "regMode": a.reg_mode,
        "status": a.status,
    }


def activity_admin_dict(a: Activity, db: Session | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        **activity_public_info(a),
        "inviteCode": a.invite_code,
        "fieldsConfig": a.fields_config,
        "allowOnsiteCombo": a.allow_onsite_combo,
        "regEnabled": a.reg_enabled,
        "checkinEnabled": a.checkin_enabled,
        "createdAt": a.created_at,
        "updatedAt": a.updated_at,
        "qrUrl": f"{settings.public_base_url}/p/{a.id}",
    }
    if db is not None:
        out["regCount"] = int(
            db.scalar(
                select(func.count()).select_from(Registration).where(Registration.activity_id == a.id)
            ) or 0
        )
        out["checkinCount"] = int(
            db.scalar(
                select(func.count()).select_from(CheckIn).where(CheckIn.activity_id == a.id)
            ) or 0
        )
    return out


def _validate_window(payload: ActivityWrite) -> None:
    points = [
        ("报名开始", payload.reg_start),
        ("报名结束", payload.reg_end),
        ("报到开始", payload.checkin_start),
        ("报到结束", payload.checkin_end),
    ]
    parsed = []
    for label, raw in points:
        dt = parse_dt(raw)
        if dt is None:
            raise ServiceError(f"{label}时间格式不正确，请使用 ISO8601（如 2026-11-20T09:00:00+08:00）")
        parsed.append((label, dt))
    for i in range(len(parsed) - 1):
        if parsed[i][1] > parsed[i + 1][1]:
            raise ServiceError(
                f"时间顺序不正确：{parsed[i][0]} 不能晚于 {parsed[i + 1][0]}"
            )
    if payload.reg_mode == "invite" and not payload.invite_code.strip():
        raise ServiceError("邀请码模式必须设置邀请码")


def create_activity(db: Session, payload: ActivityWrite) -> Activity:
    _validate_window(payload)
    now = _now_iso()
    a = Activity(
        id=generate_short_id(db),
        name=payload.name,
        description=payload.description,
        location=payload.location,
        reg_start=payload.reg_start.strip(),
        reg_end=payload.reg_end.strip(),
        checkin_start=payload.checkin_start.strip(),
        checkin_end=payload.checkin_end.strip(),
        status=payload.status,
        reg_mode=payload.reg_mode,
        invite_code=payload.invite_code.strip(),
        fields_config=payload.fields_config.model_dump(),
        allow_onsite_combo=payload.allow_onsite_combo,
        reg_enabled=payload.reg_enabled,
        checkin_enabled=payload.checkin_enabled,
        created_at=now,
        updated_at=now,
    )
    db.add(a)
    db.commit()
    return a


def update_activity(db: Session, a: Activity, payload: ActivityWrite) -> Activity:
    _validate_window(payload)
    a.name = payload.name
    a.description = payload.description
    a.location = payload.location
    a.reg_start = payload.reg_start.strip()
    a.reg_end = payload.reg_end.strip()
    a.checkin_start = payload.checkin_start.strip()
    a.checkin_end = payload.checkin_end.strip()
    a.status = payload.status
    a.reg_mode = payload.reg_mode
    a.invite_code = payload.invite_code.strip()
    a.fields_config = payload.fields_config.model_dump()
    a.allow_onsite_combo = payload.allow_onsite_combo
    a.reg_enabled = payload.reg_enabled
    a.checkin_enabled = payload.checkin_enabled
    a.updated_at = _now_iso()
    db.commit()
    return a


def list_activities(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(select(Activity).order_by(Activity.created_at.desc())).all()
    return [activity_admin_dict(a, db) for a in rows]


def delete_activity(db: Session, a: Activity) -> None:
    aid = a.id
    for model in (Whitelist, CheckIn, Registration):
        db.execute(delete(model).where(model.activity_id == aid))
    db.delete(a)
    db.commit()


def get_status(db: Session, a: Activity, phone_raw: str | None) -> dict[str, Any]:
    if a.status == "draft":
        result = determine_stage(a, now_shanghai(), USER_UNKNOWN)
        return {
            **result.as_dict(),
            "action": "info",
            "message": "活动尚未发布，请稍后再来",
            "fieldsConfig": a.fields_config,
            "activityInfo": activity_public_info(a),
        }
    if a.status == "ended":
        result = determine_stage(a, now_shanghai(), USER_UNKNOWN)
        return {
            **result.as_dict(),
            "action": "info",
            "message": "活动已结束，感谢参与",
            "fieldsConfig": a.fields_config,
            "activityInfo": activity_public_info(a),
        }

    user_state = USER_UNKNOWN
    if phone_raw:
        try:
            ph = cipher.digest(normalize_phone(phone_raw))
        except ValueError:
            ph = None
        if ph:
            if db.scalar(
                select(CheckIn.id).where(
                    CheckIn.activity_id == a.id, CheckIn.phone_hash == ph
                )
            ):
                user_state = USER_CHECKED_IN
            elif db.scalar(
                select(Registration.id).where(
                    Registration.activity_id == a.id, Registration.phone_hash == ph
                )
            ):
                user_state = USER_REGISTERED
            else:
                user_state = USER_NOT_REGISTERED

    result = determine_stage(a, now_shanghai(), user_state)
    return {
        **result.as_dict(),
        "fieldsConfig": a.fields_config,
        "activityInfo": activity_public_info(a),
    }


def _check_eligibility(db: Session, a: Activity, phone_hash: str, invite_code: str) -> None:
    if a.reg_mode == "whitelist":
        hit = db.scalar(
            select(Whitelist.id).where(
                Whitelist.activity_id == a.id, Whitelist.phone_hash == phone_hash
            )
        )
        if not hit:
            raise ServiceError("您不在本次活动邀请名单内，请联系组织者", 403)
    elif a.reg_mode == "invite":
        expected = (a.invite_code or "").strip()
        if not expected or invite_code.strip() != expected:
            raise ServiceError("邀请码不正确，请联系组织者", 403)


def _validate_submission(fields: dict, data: RegisterRequest | CheckinRequest) -> dict[str, str]:
    errors: list[str] = []
    name = (data.name or "").strip()
    category = (data.category or "").strip()
    if not name:
        errors.append("姓名不能为空")
    options = [str(x) for x in (fields.get("type_options") or [])]
    if category and category not in options:
        errors.append("类型不在可选范围内")
    elif not category:
        errors.append("类型不能为空")

    clean = {
        "name": name,
        "category": category,
        "department": "",
        "unit": "",
        "remark": "",
    }
    for key in ("department", "unit", "remark"):
        cfg = fields.get(key) or {}
        enabled = bool(cfg.get("enabled", False))
        required = enabled and bool(cfg.get("required", False))
        value = (getattr(data, key, "") or "").strip()
        if required and not value:
            errors.append(f"{_FIELD_LABELS[key]}不能为空")
        clean[key] = value if enabled else ""
    if errors:
        raise ServiceError("；".join(errors))
    return clean


def _registration_dict(
    reg: Registration, a: Activity, include_full: bool = False
) -> dict[str, Any]:
    """参与者侧只返回脱敏手机号；明文仅限管理端明细/导出（鉴权保护）。"""
    try:
        phone = cipher.decrypt(reg.phone_enc)
    except ValueError:
        phone = ""
    item: dict[str, Any] = {
        "id": reg.id,
        "name": reg.name,
        "phone": cipher.mask(phone),
        "category": reg.category,
        "department": reg.department,
        "unit": reg.unit,
        "remark": reg.remark,
        "source": reg.source,
        "createdAt": reg.created_at,
        "activityName": a.name,
    }
    if include_full:
        item["phoneFull"] = phone
    return item


def register(db: Session, a: Activity, payload: RegisterRequest) -> dict[str, Any]:
    with _WRITE_LOCK:
        return _register_impl(db, a, payload)


def _register_impl(db: Session, a: Activity, payload: RegisterRequest) -> dict[str, Any]:
    try:
        phone = normalize_phone(payload.phone)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc
    ph = cipher.digest(phone)

    existing = db.scalar(
        select(Registration).where(
            Registration.activity_id == a.id, Registration.phone_hash == ph
        )
    )
    if existing is not None:
        checked = db.scalar(
            select(CheckIn.id).where(
                CheckIn.activity_id == a.id, CheckIn.phone_hash == ph
            )
        )
        return {
            "ok": True,
            "status": "already_registered",
            "checkedIn": checked is not None,
            "message": "您已完成报到，无需重复操作" if checked else "您已报名成功，请勿重复提交",
            "registration": _registration_dict(existing, a),
        }

    if a.status != "active":
        raise ServiceError("活动当前不可用，请联系组织者", 403)
    _check_eligibility(db, a, ph, payload.invite_code)
    clean = _validate_submission(a.fields_config, payload)

    stage = determine_stage(a, now_shanghai(), USER_NOT_REGISTERED)
    if stage.stage == STAGE_NOT_STARTED:
        raise ServiceError("报名未开始，请在报名开放后扫码")
    if stage.stage == STAGE_ENDED:
        raise ServiceError("活动已结束，无法报名")
    if stage.stage == STAGE_WAITING:
        raise ServiceError("报名已截止，如有疑问请联系组织者")
    if stage.stage == STAGE_REGISTRATION and not a.reg_enabled:
        raise ServiceError("报名通道已由组织者暂时关闭")
    combo = stage.stage == STAGE_CHECKIN and stage.combo
    if stage.stage == STAGE_CHECKIN and (not combo or not a.checkin_enabled):
        raise ServiceError("当前为报到时段且未开放现场报名，请先完成报名")

    now = _now_iso()
    try:
        reg = Registration(
            activity_id=a.id,
            name=clean["name"],
            phone_enc=cipher.encrypt(phone),
            phone_hash=ph,
            category=clean["category"],
            department=clean["department"],
            unit=clean["unit"],
            remark=clean["remark"],
            extra={},
            source=payload.source or "qr",
            status="active",
            created_at=now,
        )
        db.add(reg)
        db.flush()
        if combo:
            db.add(
                CheckIn(
                    activity_id=a.id,
                    registration_id=reg.id,
                    phone_hash=ph,
                    method="combo",
                    created_at=now,
                )
            )
        db.commit()
    except IntegrityError:
        db.rollback()
        again = db.scalar(
            select(Registration).where(
                Registration.activity_id == a.id, Registration.phone_hash == ph
            )
        )
        if again is not None:
            return {
                "ok": True,
                "status": "already_registered",
                "checkedIn": False,
                "message": "您已报名成功，请勿重复提交",
                "registration": _registration_dict(again, a),
            }
        raise ServiceError("提交冲突，请重试", 409) from None

    return {
        "ok": True,
        "status": "registered_and_checked_in" if combo else "registered",
        "checkedIn": combo,
        "message": "现场报名与报到已完成" if combo else "报名成功，请在活动开始后扫码报到",
        "registration": _registration_dict(reg, a),
    }


def checkin(db: Session, a: Activity, payload: CheckinRequest) -> dict[str, Any]:
    with _WRITE_LOCK:
        return _checkin_impl(db, a, payload)


def _checkin_impl(db: Session, a: Activity, payload: CheckinRequest) -> dict[str, Any]:
    try:
        phone = normalize_phone(payload.phone)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc
    ph = cipher.digest(phone)

    existing_ci = db.scalar(
        select(CheckIn).where(CheckIn.activity_id == a.id, CheckIn.phone_hash == ph)
    )
    if existing_ci is not None:
        reg = db.get(Registration, existing_ci.registration_id)
        return {
            "ok": True,
            "status": "already_checked_in",
            "checkedIn": True,
            "message": "您已完成报到，无需重复操作",
            "registration": _registration_dict(reg, a) if reg else None,
        }

    if a.status != "active":
        raise ServiceError("活动当前不可用，请联系组织者", 403)
    stage = determine_stage(a, now_shanghai(), USER_NOT_REGISTERED)
    if stage.stage != STAGE_CHECKIN:
        raise ServiceError("当前不在报到时间，请在活动开始后扫码报到")
    if not a.checkin_enabled:
        raise ServiceError("报到通道已由组织者暂时关闭")

    reg = db.scalar(
        select(Registration).where(
            Registration.activity_id == a.id, Registration.phone_hash == ph
        )
    )
    is_combo = reg is None
    if is_combo:
        if not stage.combo:
            raise ServiceError("本活动未开放现场报名，请先完成报名后再扫码报到")
        _check_eligibility(db, a, ph, payload.invite_code)
        clean = _validate_submission(a.fields_config, payload)

    now = _now_iso()
    try:
        if is_combo:
            reg = Registration(
                activity_id=a.id,
                name=clean["name"],
                phone_enc=cipher.encrypt(phone),
                phone_hash=ph,
                category=clean["category"],
                department=clean["department"],
                unit=clean["unit"],
                remark=clean["remark"],
                extra={},
                source=payload.source or "qr",
                status="active",
                created_at=now,
            )
            db.add(reg)
            db.flush()
        ci = CheckIn(
            activity_id=a.id,
            registration_id=reg.id,
            phone_hash=ph,
            method="combo" if is_combo else "qr",
            created_at=now,
        )
        db.add(ci)
        db.commit()
    except IntegrityError:
        db.rollback()
        again = db.scalar(
            select(CheckIn).where(CheckIn.activity_id == a.id, CheckIn.phone_hash == ph)
        )
        if again is not None:
            reg = db.get(Registration, again.registration_id)
            return {
                "ok": True,
                "status": "already_checked_in",
                "checkedIn": True,
                "message": "您已完成报到，无需重复操作",
                "registration": _registration_dict(reg, a) if reg else None,
            }
        raise ServiceError("提交冲突，请重试", 409) from None

    return {
        "ok": True,
        "status": "checked_in" if not is_combo else "registered_and_checked_in",
        "checkedIn": True,
        "message": "报到成功，欢迎参加本次活动",
        "registration": _registration_dict(reg, a),
    }


def stats(db: Session, a: Activity) -> dict[str, Any]:
    reg_count = int(
        db.scalar(
            select(func.count()).select_from(Registration).where(Registration.activity_id == a.id)
        ) or 0
    )
    checkin_count = int(
        db.scalar(
            select(func.count()).select_from(CheckIn).where(CheckIn.activity_id == a.id)
        ) or 0
    )
    regs = db.scalars(
        select(Registration)
        .where(Registration.activity_id == a.id)
        .order_by(Registration.created_at.desc(), Registration.id.desc())
        .limit(8)
    ).all()
    cis = db.scalars(
        select(CheckIn)
        .where(CheckIn.activity_id == a.id)
        .order_by(CheckIn.created_at.desc(), CheckIn.id.desc())
        .limit(8)
    ).all()
    # 直接构造带姓名的动态条目：2合1 会同时产生报名与报到两条相同时间戳的记录，
    # 靠 time 字符串回填姓名会错配，且循环内 db.get 属于 N+1 查询。
    recent: list[dict[str, Any]] = [
        {"type": "register", "name": r.name, "time": r.created_at, "seq": r.id}
        for r in regs
    ]
    for c in cis:
        reg = db.get(Registration, c.registration_id)
        recent.append(
            {
                "type": "checkin",
                "name": reg.name if reg else "现场报名",
                "time": c.created_at,
                "seq": c.id,
            }
        )
    recent.sort(key=lambda x: (x["time"], x["seq"]), reverse=True)
    return {
        "activityId": a.id,
        "regCount": reg_count,
        "checkinCount": checkin_count,
        "pendingCount": max(reg_count - checkin_count, 0),
        "checkinRate": round(checkin_count * 100.0 / reg_count, 1) if reg_count else 0.0,
        "serverTime": _now_iso(),
        "recent": [
            {"type": x["type"], "name": x["name"], "time": x["time"]}
            for x in recent[:10]
        ],
    }


def details(
    db: Session,
    a: Activity,
    search: str = "",
    status: str = "all",
    category: str = "",
    order: str = "desc",
) -> list[dict[str, Any]]:
    regs = db.scalars(
        select(Registration).where(Registration.activity_id == a.id)
    ).all()
    ci_ids = {
        r.registration_id
        for r in db.scalars(select(CheckIn).where(CheckIn.activity_id == a.id)).all()
    }
    out: list[dict[str, Any]] = []
    keyword = (search or "").strip()
    for r in regs:
        item = _registration_dict(r, a, include_full=True)
        item["checkedIn"] = r.id in ci_ids
        if keyword:
            try:
                kw_hash = cipher.digest(normalize_phone(keyword))
            except ValueError:
                kw_hash = None
            hit = keyword in r.name or (kw_hash is not None and kw_hash == r.phone_hash)
            if not hit:
                continue
        if status == "checked_in" and not item["checkedIn"]:
            continue
        if status == "pending" and item["checkedIn"]:
            continue
        if category and item["category"] != category:
            continue
        out.append(item)
    out.sort(key=lambda x: (x["createdAt"], x["id"]), reverse=order != "asc")
    return out


def export_bytes(db: Session, a: Activity, fmt: str = "xlsx") -> tuple[bytes, str, str]:
    rows = details(db, a, order="asc")
    headers = ["序号", "姓名", "手机号", "类型", "所属部门", "单位", "备注", "报名时间", "报到状态", "报名来源"]
    body: list[list[Any]] = []
    for i, r in enumerate(rows, 1):
        body.append([
            i, r["name"], r["phoneFull"], r["category"], r["department"], r["unit"],
            r["remark"], r["createdAt"], "已报到" if r["checkedIn"] else "未报到", r["source"],
        ])
    filename = f"activity_{a.id}_{now_shanghai().strftime('%Y%m%d_%H%M%S')}"
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        writer.writerows(body)
        return ("\ufeff" + buf.getvalue()).encode("utf-8"), f"{filename}.csv", "text/csv; charset=utf-8"

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "报名与报到明细"
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2F5597")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row in body:
        ws.append(row)
    widths = [6, 14, 16, 12, 20, 24, 30, 24, 12, 12]
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    out = io.BytesIO()
    wb.save(out)
    return (
        out.getvalue(),
        f"{filename}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def qrcode_bytes(a: Activity, size: int = 10, fmt: str = "png") -> tuple[bytes, str]:
    url = f"{settings.public_base_url}/p/{a.id}"
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_H,
        box_size=size,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.BytesIO()
    if fmt == "svg":
        img = qr.make_image(image_factory=SvgPathImage)
        img.save(buf)
        return buf.getvalue(), "image/svg+xml"
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(buf, format="PNG")
    return buf.getvalue(), "image/png"


def get_whitelist(db: Session, a: Activity) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(Whitelist).where(Whitelist.activity_id == a.id).order_by(Whitelist.id)
    ).all()
    return [{"id": w.id, "name": w.name, "phoneHash": w.phone_hash[:12]} for w in rows]


def replace_whitelist(db: Session, a: Activity, payload: WhitelistWrite) -> int:
    seen: dict[str, str] = {}
    for item in payload.items:
        try:
            phone = normalize_phone(item.phone)
        except ValueError as exc:
            raise ServiceError(f"白名单条目 {item.phone!r} 格式不正确：{exc}") from exc
        seen[cipher.digest(phone)] = item.name.strip()
    db.query(Whitelist).filter(Whitelist.activity_id == a.id).delete()
    now = _now_iso()
    for ph, name in seen.items():
        db.add(Whitelist(activity_id=a.id, name=name, phone_hash=ph, created_at=now))
    db.commit()
    return len(seen)
