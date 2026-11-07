"""M1 后端 API 全面验证：需求第 8 节 9 个 API、幂等、2合1、资格、鉴权、导出、二维码。"""
from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor

from PIL import Image


def reg(phone="13800138000", **kw):
    base = {
        "phone": phone,
        "name": "张三",
        "category": "普通",
        "department": "技术部",
        "unit": "测试单位",
        "remark": "",
    }
    base.update(kw)
    return base


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["tz"] == "Asia/Shanghai"
    assert body["database"] == "sqlite"


def test_login_and_auth_required(client, auth):
    r = client.post("/api/admin/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
    assert client.get("/api/admin/activities").status_code == 401
    assert client.get("/api/admin/activities", headers=auth).status_code == 200


def test_create_list_get_update_delete(client, auth, create_activity, activity_payload):
    a = create_activity(name="秋季大会")
    assert len(a["id"]) == 6
    assert a["qrUrl"] == f"https://qr.example.com/p/{a['id']}"
    assert a["allowOnsiteCombo"] is True

    r = client.get("/api/admin/activities", headers=auth)
    assert r.status_code == 200 and len(r.json()["items"]) == 1

    r = client.get(f"/api/admin/activities/{a['id']}", headers=auth)
    assert r.json()["activity"]["name"] == "秋季大会"

    payload = activity_payload(name="改名后", allow_onsite_combo=False)
    r = client.put(f"/api/admin/activities/{a['id']}", json=payload, headers=auth)
    assert r.status_code == 200
    assert r.json()["activity"]["name"] == "改名后"
    assert r.json()["activity"]["allowOnsiteCombo"] is False

    r = client.delete(f"/api/admin/activities/{a['id']}", headers=auth)
    assert r.status_code == 200
    assert client.get("/api/admin/activities", headers=auth).json()["items"] == []
    assert client.post(f"/api/activity/{a['id']}/status", json={}).status_code == 404


def test_window_validation(client, auth, activity_payload):
    bad = activity_payload(reg_start=5, reg_end=1)
    r = client.post("/api/admin/activities", json=bad, headers=auth)
    assert r.status_code == 400 and "顺序" in r.json()["message"]
    bad2 = activity_payload(reg_mode="invite", invite_code="")
    assert client.post("/api/admin/activities", json=bad2, headers=auth).status_code == 400


def test_status_unknown_and_registration_form(client, create_activity):
    a = create_activity()
    r = client.post(f"/api/activity/{a['id']}/status", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["stage"] == "registration"
    assert body["userState"] == "unknown"
    assert body["needsPhone"] is True
    assert body["action"] == "form"
    assert body["fieldsConfig"]["type_options"] == ["普通", "VIP", "其他"]
    assert "+08:00" in body["serverTime"]

    r2 = client.post(f"/api/activity/{a['id']}/status", json={"phone": "13900139000"})
    assert r2.json()["userState"] == "not_registered"
    assert r2.json()["needsPhone"] is False


def test_register_success_and_idempotency(client, auth, create_activity):
    a = create_activity()
    url = f"/api/activity/{a['id']}"
    r = client.post(f"{url}/register", json=reg())
    assert r.status_code == 200
    assert r.json()["status"] == "registered"
    assert r.json()["registration"]["phone"] == "138****8000"
    assert "13800138000" not in r.text
    # 审查修复：凭证时间必须由服务端给出（Asia/Shanghai），不依赖客户端时钟
    assert "+08:00" in r.json()["serverTime"]

    r2 = client.post(f"{url}/register", json=reg())
    assert r2.status_code == 200
    assert r2.json()["status"] == "already_registered"

    stats = client.get(f"/api/admin/activities/{a['id']}/stats", headers=auth).json()["stats"]
    assert stats["regCount"] == 1 and stats["checkinCount"] == 0

    st = client.post(f"{url}/status", json={"phone": "13800138000"}).json()
    assert st["userState"] == "registered"
    assert st["action"] == "message" and "已报名" in st["message"]


def test_field_validation(client, create_activity):
    a = create_activity()
    url = f"/api/activity/{a['id']}/register"
    assert client.post(url, json=reg(name="")).status_code == 400
    assert client.post(url, json=reg(category="不存在的类型")).status_code == 400
    assert client.post(url, json=reg(phone="123")).status_code == 400
    assert client.post(url, json=reg(phone="138-0013-8000")).status_code == 200

    strict = create_activity(
        name="必填部门",
        fields_config={
            "type_options": ["普通", "VIP"],
            "department": {"enabled": True, "required": True},
            "unit": {"enabled": True, "required": False},
            "remark": {"enabled": False, "required": False},
        },
    )
    r = client.post(f"/api/activity/{strict['id']}/register", json=reg(department=""))
    assert r.status_code == 400 and "所属部门" in r.json()["message"]


def test_fields_get_put(client, auth, create_activity):
    a = create_activity()
    base = f"/api/admin/activities/{a['id']}"
    assert client.get(f"{base}/fields", headers=auth).json()["fieldsConfig"]["type_options"] == [
        "普通", "VIP", "其他"
    ]
    cfg = {
        "type_options": ["嘉宾", "观众"],
        "department": {"enabled": False, "required": False},
        "unit": {"enabled": True, "required": True},
        "remark": {"enabled": True, "required": False},
    }
    r = client.put(f"{base}/fields", json=cfg, headers=auth)
    assert r.status_code == 200 and r.json()["fieldsConfig"]["type_options"] == ["嘉宾", "观众"]
    r2 = client.post(f"/api/activity/{a['id']}/status", json={})
    assert r2.json()["fieldsConfig"]["unit"]["required"] is True


def test_whitelist_and_invite_mode(client, auth, create_activity):
    wl = create_activity(name="白名单活动", reg_mode="whitelist")
    url = f"/api/activity/{wl['id']}/register"
    assert client.post(url, json=reg()).status_code == 403
    r = client.put(
        f"/api/admin/activities/{wl['id']}/whitelist",
        json={"items": [{"name": "张三", "phone": "138 0013 8000"}]},
        headers=auth,
    )
    assert r.status_code == 200 and r.json()["count"] == 1
    assert client.post(url, json=reg()).status_code == 200
    items = client.get(
        f"/api/admin/activities/{wl['id']}/whitelist", headers=auth
    ).json()["items"]
    assert len(items) == 1
    assert client.put(
        f"/api/admin/activities/{wl['id']}/whitelist",
        json={"items": [{"name": "x", "phone": "bad"}]},
        headers=auth,
    ).status_code == 400

    inv = create_activity(name="邀请码活动", reg_mode="invite", invite_code="VIP2026")
    iurl = f"/api/activity/{inv['id']}/register"
    assert client.post(iurl, json=reg(phone="13900139000")).status_code == 403
    assert client.post(iurl, json=reg(phone="13900139000", invite_code="VIP2026")).status_code == 200


def test_checkin_flow_and_idempotency(client, auth, create_activity):
    a = create_activity(reg_start=-5, reg_end=-3, checkin_start=-1, checkin_end=1)
    url = f"/api/activity/{a['id']}"
    # 先在报名期补一条报名（时间窗已过，改用服务层直接构造）
    from app.crypto import cipher
    from app.db import SessionLocal
    from app.models import Registration
    from app.stage import iso_shanghai, now_shanghai

    with SessionLocal() as db:
        db.add(
            Registration(
                activity_id=a["id"], name="李四", phone_enc=cipher.encrypt("13700137000"),
                phone_hash=cipher.digest("13700137000"), category="VIP", department="",
                unit="", remark="", extra={}, source="qr", status="active",
                created_at=iso_shanghai(now_shanghai()),
            )
        )
        db.commit()

    st = client.post(f"{url}/status", json={"phone": "13700137000"}).json()
    assert st["stage"] == "checkin" and st["action"] == "button"

    r = client.post(f"{url}/checkin", json={"phone": "13700137000"})
    assert r.status_code == 200 and r.json()["status"] == "checked_in"
    r2 = client.post(f"{url}/checkin", json={"phone": "13700137000"})
    assert r2.json()["status"] == "already_checked_in"

    stats = client.get(f"/api/admin/activities/{a['id']}/stats", headers=auth).json()["stats"]
    assert stats["regCount"] == 1 and stats["checkinCount"] == 1
    assert stats["checkinRate"] == 100.0
    assert client.post(f"{url}/status", json={"phone": "13700137000"}).json()["userState"] == "checked_in"


def test_onsite_combo_single_transaction(client, auth, create_activity):
    a = create_activity(reg_start=-5, reg_end=-3, checkin_start=-1, checkin_end=1)
    url = f"/api/activity/{a['id']}"
    st = client.post(f"{url}/status", json={"phone": "13600136000"}).json()
    assert st["action"] == "combo_form" and st["combo"] is True
    r = client.post(f"{url}/register", json=reg(phone="13600136000", name="王五"))
    assert r.json()["status"] == "registered_and_checked_in"
    stats = client.get(f"/api/admin/activities/{a['id']}/stats", headers=auth).json()["stats"]
    assert stats["regCount"] == 1 and stats["checkinCount"] == 1

    # 2合1 关闭：未报名者不能现场报到
    a2 = create_activity(
        name="关闭2合1", reg_start=-5, reg_end=-3, checkin_start=-1, checkin_end=1,
        allow_onsite_combo=False,
    )
    r2 = client.post(f"/api/activity/{a2['id']}/checkin", json=reg(phone="13500135000"))
    assert r2.status_code == 400 and "未开放现场报名" in r2.json()["message"]


def test_manual_switches_and_closed_stages(client, create_activity):
    a = create_activity(name="报名关闭", reg_enabled=False)
    r = client.post(f"/api/activity/{a['id']}/register", json=reg())
    assert r.status_code == 400 and "关闭" in r.json()["message"]

    future = create_activity(name="未开始", reg_start=2, reg_end=3, checkin_start=4, checkin_end=5)
    r2 = client.post(f"/api/activity/{future['id']}/register", json=reg())
    assert r2.status_code == 400 and "未开始" in r2.json()["message"]
    st = client.post(f"/api/activity/{future['id']}/status", json={}).json()
    assert st["stage"] == "not_started"

    past = create_activity(name="已结束", reg_start=-9, reg_end=-8, checkin_start=-7, checkin_end=-6)
    r3 = client.post(f"/api/activity/{past['id']}/register", json=reg())
    assert r3.status_code == 400 and "结束" in r3.json()["message"]
    assert client.post(f"/api/activity/{past['id']}/status", json={}).json()["stage"] == "ended"


def test_details_search_filter_and_export(client, auth, create_activity):
    a = create_activity()
    url = f"/api/activity/{a['id']}"
    client.post(f"{url}/register", json=reg(phone="13800138000", name="张三", category="VIP"))
    client.post(f"{url}/register", json=reg(phone="13900139000", name="李四", category="普通"))

    r = client.get(f"/api/admin/activities/{a['id']}/details", headers=auth)
    assert len(r.json()["items"]) == 2
    r2 = client.get(
        f"/api/admin/activities/{a['id']}/details?search=13800138000", headers=auth
    )
    assert len(r2.json()["items"]) == 1 and r2.json()["items"][0]["name"] == "张三"
    r3 = client.get(
        f"/api/admin/activities/{a['id']}/details?category=VIP", headers=auth
    )
    assert len(r3.json()["items"]) == 1
    r4 = client.get(
        f"/api/admin/activities/{a['id']}/details?status=pending", headers=auth
    )
    assert len(r4.json()["items"]) == 2
    r5 = client.get(
        f"/api/admin/activities/{a['id']}/details?order=asc", headers=auth
    )
    assert r5.json()["items"][0]["name"] == "张三"

    xlsx = client.get(f"/api/admin/activities/{a['id']}/export", headers=auth)
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(xlsx.content))
    sheet = wb.active
    assert sheet.max_row == 3
    assert sheet.cell(1, 2).value == "姓名"
    assert "13800138000" in {str(sheet.cell(r, 3).value) for r in range(2, 4)}
    csv_r = client.get(f"/api/admin/activities/{a['id']}/export?format=csv", headers=auth)
    assert csv_r.status_code == 200 and "姓名" in csv_r.content.decode("utf-8-sig")
    assert "13800138000" in csv_r.content.decode("utf-8-sig")


def test_qrcode_png_and_svg(client, auth, create_activity):
    a = create_activity()
    base = f"/api/admin/activities/{a['id']}/qrcode"
    png = client.get(base, headers=auth)
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(png.content))
    w1 = img.size[0]
    big = client.get(f"{base}?size=20", headers=auth)
    assert Image.open(io.BytesIO(big.content)).size[0] > w1
    svg = client.get(f"{base}?format=svg", headers=auth)
    assert svg.status_code == 200 and b"<svg" in svg.content[:400]
    assert client.get(f"{base}?size=99", headers=auth).status_code == 400


def test_draft_and_ended_status(client, auth, create_activity, activity_payload):
    a = create_activity(name="草稿", status="draft")
    st = client.post(f"/api/activity/{a['id']}/status", json={}).json()
    assert st["action"] == "info" and "尚未发布" in st["message"]
    assert client.post(f"/api/activity/{a['id']}/register", json=reg()).status_code == 403


def test_rate_limit(client, create_activity):
    from app.security import submit_limiter

    a = create_activity()
    url = f"/api/activity/{a['id']}/register"
    original = submit_limiter.max_calls
    submit_limiter.max_calls = 2
    try:
        codes = [client.post(url, json=reg(phone=f"1380013800{i}")).status_code for i in range(3)]
        assert codes[:2] == [200, 200]
        assert codes[2] == 429
    finally:
        submit_limiter.max_calls = original


def test_concurrent_register_is_idempotent(client, auth, create_activity):
    a = create_activity()
    url = f"/api/activity/{a['id']}/register"

    def submit(_):
        return client.post(url, json=reg()).status_code

    with ThreadPoolExecutor(max_workers=5) as pool:
        codes = list(pool.map(submit, range(5)))
    assert all(c == 200 for c in codes)
    stats = client.get(f"/api/admin/activities/{a['id']}/stats", headers=auth).json()["stats"]
    assert stats["regCount"] == 1


# ---- 独立审查后补充的回归用例（P0/P1 修复验证与盲点覆盖）----


def test_no_plaintext_phone_in_participant_response(client, create_activity):
    a = create_activity()
    r = client.post(f"/api/activity/{a['id']}/register", json=reg())
    assert r.status_code == 200
    assert "13800138000" not in r.text
    assert r.json()["registration"]["phone"] == "138****8000"
    assert "phoneFull" not in r.json()["registration"]


def test_forged_xff_cannot_bypass_rate_limit(client, create_activity):
    from app.security import submit_limiter

    a = create_activity()
    url = f"/api/activity/{a['id']}/register"
    original = submit_limiter.max_calls
    submit_limiter.max_calls = 2
    try:
        codes = []
        for i in range(3):
            codes.append(
                client.post(
                    url,
                    json=reg(phone=f"1380013800{i}"),
                    headers={"X-Forwarded-For": f"10.0.0.{i}"},
                ).status_code
            )
        assert codes == [200, 200, 429]
    finally:
        submit_limiter.max_calls = original


def test_login_rate_limit(client):
    from app.security import login_limiter

    original = login_limiter.max_calls
    login_limiter.max_calls = 3
    try:
        codes = [
            client.post(
                "/api/admin/login",
                json={"username": "admin", "password": "wrong"},
            ).status_code
            for _ in range(4)
        ]
        assert codes[:3] == [401, 401, 401]
        assert codes[3] == 429
    finally:
        login_limiter.max_calls = original


def test_admin_endpoints_require_token(client, create_activity):
    a = create_activity()
    base = f"/api/admin/activities/{a['id']}"
    for path in ("/stats", "/details", "/export", "/qrcode", "/fields", "/whitelist"):
        assert client.get(base + path).status_code == 401


def test_waiting_gap_and_checkin_disabled(client, create_activity):
    gap = create_activity(
        name="报名已截止", reg_start=-5, reg_end=-3, checkin_start=2, checkin_end=4
    )
    st = client.post(f"/api/activity/{gap['id']}/status", json={}).json()
    assert st["stage"] == "waiting" and st["action"] == "message"
    r = client.post(f"/api/activity/{gap['id']}/register", json=reg())
    assert r.status_code == 400 and "截止" in r.json()["message"]

    off = create_activity(
        name="报到关闭", reg_start=-5, reg_end=-3, checkin_start=-1, checkin_end=1,
        checkin_enabled=False,
    )
    st2 = client.post(f"/api/activity/{off['id']}/status", json={}).json()
    assert st2["action"] == "message" and "关闭" in st2["message"]
    r2 = client.post(f"/api/activity/{off['id']}/checkin", json=reg())
    assert r2.status_code == 400


def test_invalid_window_format(client, auth, activity_payload):
    bad = activity_payload(reg_start="not-a-date")
    r = client.post("/api/admin/activities", json=bad, headers=auth)
    assert r.status_code == 400 and "格式" in r.json()["message"]


def test_combo_respects_whitelist(client, auth, create_activity):
    a = create_activity(
        name="白名单2合1", reg_start=-5, reg_end=-3, checkin_start=-1, checkin_end=1,
        reg_mode="whitelist",
    )
    base = f"/api/activity/{a['id']}"
    assert client.post(f"{base}/register", json=reg()).status_code == 403
    client.put(
        f"/api/admin/activities/{a['id']}/whitelist",
        json={"items": [{"name": "张三", "phone": "13800138000"}]},
        headers=auth,
    )
    r = client.post(f"{base}/register", json=reg())
    assert r.status_code == 200 and r.json()["status"] == "registered_and_checked_in"


def test_stats_recent_names_are_correct(client, auth, create_activity):
    a = create_activity(reg_start=-5, reg_end=-3, checkin_start=-1, checkin_end=1)
    client.post(
        f"/api/activity/{a['id']}/register", json=reg(phone="13800138000", name="王五")
    )
    recent = client.get(
        f"/api/admin/activities/{a['id']}/stats", headers=auth
    ).json()["stats"]["recent"]
    assert len(recent) == 2
    assert {x["type"] for x in recent} == {"register", "checkin"}
    checkin_item = next(x for x in recent if x["type"] == "checkin")
    assert checkin_item["name"] == "王五"
