"""FastAPI 应用：标准同源 REST + 静态页面托管。

同源约定：/p/* 参与者页、/admin 后台、/api/* 接口、/static/* 资源，
全部挂在同一域名下 —— 无 CORS、无多域名备案、无混合内容、无跨域风控。
生产可将 SERVE_STATIC=0，由 CloudBase 静态托管服务页面，API 走 HTTP 云函数。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import BASE_DIR, settings
from .db import get_db, init_db
from .models import Activity, Admin
from .schemas import (
    ActivityWrite,
    CheckinRequest,
    FieldsConfig,
    LoginRequest,
    RegisterRequest,
    StatusRequest,
    WhitelistWrite,
)
from .security import (
    client_ip,
    general_limiter,
    issue_token,
    login_limiter,
    require_admin,
    submit_limiter,
    verify_password,
)
from .services import (
    ServiceError,
    activity_admin_dict,
    checkin,
    create_activity,
    delete_activity,
    details,
    export_bytes,
    get_status,
    get_whitelist,
    list_activities,
    qrcode_bytes,
    register,
    replace_whitelist,
    stats,
    update_activity,
)
from .stage import iso_shanghai, now_shanghai

logger = logging.getLogger("qr4events")
STATIC_DIR = BASE_DIR / "app" / "static"


class _RequestTooLarge(Exception):
    pass


class RequestSizeLimitMiddleware:
    """Bound API request memory use, including chunked bodies without Content-Length."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = 0
        if declared > self.max_bytes:
            await self._reject(send)
            return

        total = 0
        response_started = False

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    raise _RequestTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestTooLarge:
            if not response_started:
                await self._reject(send)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = '{"ok":false,"message":"请求体过大，请减少提交内容"}'.encode("utf-8")
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    if settings.admin_pass_generated:
        logger.warning(
            "未设置 ADMIN_PASS，已生成随机初始管理员口令，见 data/initial_admin_password.txt"
        )
    if settings.public_base_url.startswith("http://localhost"):
        logger.info("PUBLIC_BASE_URL 仍为 localhost，二维码仅供本地测试")
    else:
        parsed = urlparse(settings.public_base_url)
        bad = (
            parsed.scheme != "https"
            or parsed.port not in (None, 443)
            or parsed.path not in ("", "/")
        )
        if bad:
            logger.warning(
                "PUBLIC_BASE_URL 不符合「HTTPS + 标准443 + 根路径」约定，"
                "二维码可能在微信内被拦截：%s",
                settings.public_base_url,
            )
    yield


app = FastAPI(
    title="活动报名与扫码签到系统",
    version="3.0.0",
    description="平台中立通用码 · 单码双阶段 · 服务端阶段判定",
    lifespan=lifespan,
)
app.add_middleware(RequestSizeLimitMiddleware, max_bytes=settings.max_request_bytes)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    response = None
    if path.startswith("/api/"):
        ip = client_ip(request)
        if path == "/api/admin/login":
            limiter, bucket = login_limiter, "login"
        elif path.endswith(("/register", "/checkin")):
            limiter, bucket = submit_limiter, "submit"
        else:
            limiter, bucket = general_limiter, "general"
        if not limiter.check(f"{ip}:{bucket}"):
            response = JSONResponse(
                status_code=429,
                content={"ok": False, "message": "操作过于频繁，请稍后再试"},
            )
    if response is None:
        response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(ServiceError)
async def service_error_handler(_request: Request, exc: ServiceError):
    return JSONResponse(
        status_code=exc.status_code, content={"ok": False, "message": exc.message}
    )


def _friendly_validation(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "请求参数不正确"
    err = errors[0]
    loc = [str(x) for x in err.get("loc", []) if x not in ("body", "query", "path")]
    field = loc[-1] if loc else "参数"
    kind = err.get("type", "")
    if kind in {"string_too_short", "too_short", "value_error"}:
        return f"{field} 不能为空或长度不足"
    if kind == "missing":
        return f"缺少必填项：{field}"
    if kind in {"string_pattern_mismatch", "literal_error"}:
        return f"{field} 取值不正确"
    return f"{field} 格式不正确"


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError):
    """统一成 400 + 中文提示，避免前端分别处理 400/422 两套错误格式。"""
    return JSONResponse(
        status_code=400,
        content={
            "ok": False,
            "message": _friendly_validation(exc),
            "detail": jsonable_encoder(exc.errors()),
        },
    )


def _activity_or_404(db: Session, activity_id: str) -> Activity:
    activity = db.get(Activity, activity_id)
    if activity is None:
        raise HTTPException(status_code=404, detail="活动不存在或已删除")
    return activity


# ---------------------------------------------------------------- 健康检查 / 页面


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "service": "qr4events",
        "version": "3.0.0",
        "tz": "Asia/Shanghai",
        "serverTime": iso_shanghai(now_shanghai()),
        "database": "postgresql" if settings.database_url.startswith("postgresql") else "sqlite",
    }


if settings.serve_static:
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/p/{activity_id}", include_in_schema=False)
    def participant_page(activity_id: str):
        return FileResponse(STATIC_DIR / "participant.html")

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_page():
        return FileResponse(STATIC_DIR / "admin.html")

    @app.get("/privacy", include_in_schema=False)
    def privacy_page():
        return FileResponse(STATIC_DIR / "privacy.html")

    @app.get("/", include_in_schema=False)
    def root_page():
        return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------- 参与者 API（需求 8 节 1-3）


@app.post("/api/activity/{activity_id}/status")
def api_status(
    activity_id: str, payload: StatusRequest, db: Session = Depends(get_db)
):
    return {"ok": True, **get_status(db, _activity_or_404(db, activity_id), payload.phone)}


@app.post("/api/activity/{activity_id}/register")
def api_register(
    activity_id: str, payload: RegisterRequest, db: Session = Depends(get_db)
):
    result = register(db, _activity_or_404(db, activity_id), payload)
    # 凭证时间统一以服务端时间为准，避免客户端本地时间不准造成争议。
    result.setdefault("serverTime", iso_shanghai(now_shanghai()))
    return result


@app.post("/api/activity/{activity_id}/checkin")
def api_checkin(
    activity_id: str, payload: CheckinRequest, db: Session = Depends(get_db)
):
    result = checkin(db, _activity_or_404(db, activity_id), payload)
    result.setdefault("serverTime", iso_shanghai(now_shanghai()))
    return result


# ---------------------------------------------------------------- 管理员鉴权


@app.post("/api/admin/login")
def api_login(payload: LoginRequest, db: Session = Depends(get_db)):
    admin = db.scalar(select(Admin).where(Admin.username == payload.username))
    if admin is None or not verify_password(payload.password, admin.password_hash):
        raise HTTPException(status_code=401, detail="账号或密码不正确")
    return {"ok": True, "token": issue_token(admin.username), "username": admin.username}


# ---------------------------------------------------------------- 活动管理（需求 8 节 4-5）


@app.get("/api/admin/activities")
def api_list_activities(
    db: Session = Depends(get_db), _admin: str = Depends(require_admin)
):
    return {"ok": True, "items": list_activities(db)}


@app.post("/api/admin/activities")
def api_create_activity(
    payload: ActivityWrite,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    return {"ok": True, "activity": activity_admin_dict(create_activity(db, payload))}


@app.get("/api/admin/activities/{activity_id}")
def api_get_activity(
    activity_id: str,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    return {"ok": True, "activity": activity_admin_dict(_activity_or_404(db, activity_id), db)}


@app.put("/api/admin/activities/{activity_id}")
def api_update_activity(
    activity_id: str,
    payload: ActivityWrite,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    a = _activity_or_404(db, activity_id)
    return {"ok": True, "activity": activity_admin_dict(update_activity(db, a, payload), db)}


@app.delete("/api/admin/activities/{activity_id}")
def api_delete_activity(
    activity_id: str,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    delete_activity(db, _activity_or_404(db, activity_id))
    return {"ok": True, "message": "活动及其报名/报到数据已删除"}


# ---------------------------------------------------------------- 字段配置（需求 8 节 6）


@app.get("/api/admin/activities/{activity_id}/fields")
def api_get_fields(
    activity_id: str,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    return {"ok": True, "fieldsConfig": _activity_or_404(db, activity_id).fields_config}


@app.put("/api/admin/activities/{activity_id}/fields")
def api_put_fields(
    activity_id: str,
    payload: FieldsConfig,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    a = _activity_or_404(db, activity_id)
    a.fields_config = payload.model_dump()
    a.updated_at = iso_shanghai(now_shanghai())
    db.commit()
    return {"ok": True, "fieldsConfig": a.fields_config}


# ---------------------------------------------------------------- 看板 / 明细 / 导出 / 二维码（需求 8 节 7-9）


@app.get("/api/admin/activities/{activity_id}/stats")
def api_stats(
    activity_id: str,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    return {"ok": True, "stats": stats(db, _activity_or_404(db, activity_id))}


@app.get("/api/admin/activities/{activity_id}/details")
def api_details(
    activity_id: str,
    search: str = Query(default=""),
    status: str = Query(default="all", pattern="^(all|checked_in|pending)$"),
    category: str = Query(default=""),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    items, total = details(
        db, _activity_or_404(db, activity_id), search, status, category, order,
        limit=limit, offset=offset, with_total=True,
    )
    return {"ok": True, "items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/admin/activities/{activity_id}/export")
def api_export(
    activity_id: str,
    format: str = Query(default="xlsx", pattern="^(xlsx|csv)$"),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    data, filename, media = export_bytes(db, _activity_or_404(db, activity_id), format)
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/admin/activities/{activity_id}/qrcode")
def api_qrcode(
    activity_id: str,
    size: int = Query(default=10, ge=4, le=40),
    format: str = Query(default="png", pattern="^(png|svg)$"),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    data, media = qrcode_bytes(_activity_or_404(db, activity_id), size, format)
    ext = "svg" if format == "svg" else "png"
    return Response(
        content=data,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="qrcode_{activity_id}.{ext}"',
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------- 白名单 / 邀请码（FR-11）


@app.get("/api/admin/activities/{activity_id}/whitelist")
def api_get_whitelist(
    activity_id: str,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    return {"ok": True, "items": get_whitelist(db, _activity_or_404(db, activity_id))}


@app.put("/api/admin/activities/{activity_id}/whitelist")
def api_put_whitelist(
    activity_id: str,
    payload: WhitelistWrite,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    count = replace_whitelist(db, _activity_or_404(db, activity_id), payload)
    return {"ok": True, "count": count, "message": f"白名单已更新，共 {count} 人"}
