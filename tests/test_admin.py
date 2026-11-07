"""M3 组织者后台验证：DOM 契约、后端端点齐全性、harness 行为验证。"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from app.config import BASE_DIR

STATIC = BASE_DIR / "app" / "static"
HARNESS = BASE_DIR / "tests" / "admin_harness.cjs"


def _node() -> str | None:
    return os.environ.get("NODE_BIN") or shutil.which("node")


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_admin_dom_ids_complete():
    """admin.js 里 $('#id') 引用的节点必须都在 admin.html 中定义。"""
    wanted = set(re.findall(r"\$\('([\w-]+)'\)", _read("admin.js")))
    present = set(re.findall(r'id="([\w-]+)"', _read("admin.html")))
    missing = wanted - present
    assert not missing, f"admin.html 缺少 admin.js 需要的节点: {sorted(missing)}"


def test_admin_js_endpoints_exist_in_backend():
    """后台依赖的每个 API 后缀都必须在后端路由中存在。"""
    js = _read("admin.js")
    main = (BASE_DIR / "app" / "main.py").read_text(encoding="utf-8")
    for suffix in ("/stats", "/details", "/export", "/qrcode", "/fields", "/whitelist"):
        assert suffix in js, f"admin.js 未使用 {suffix}"
        assert f"{suffix}\"" in main, f"后端缺少 {suffix} 路由"
    assert "/api/admin/login" in js and "/api/admin/activities" in js
    assert "/healthz" in js, "新建活动默认时间窗应取服务端时间"


def test_admin_write_payload_uses_snake_case():
    """写入契约必须是 snake_case，且 PUT 带回原字段配置。"""
    js = _read("admin.js")
    for key in ("reg_start:", "reg_end:", "checkin_start:", "checkin_end:",
                "reg_mode:", "invite_code:", "allow_onsite_combo:",
                "reg_enabled:", "checkin_enabled:"):
        assert key in js, f"缺少写入字段 {key}"
    assert "if (a.fieldsConfig) payload.fields_config = a.fieldsConfig;" in js


def test_admin_smoke_all_dependent_endpoints(client, auth, create_activity):
    """按后台实际会发出的请求序列走一遍，确认端点齐全、契约正确。"""
    assert "serverTime" in client.get("/healthz").json()

    a = create_activity(name="后台联调活动")
    base = f"/api/admin/activities/{a['id']}"
    assert client.get("/api/admin/activities", headers=auth).json()["items"]
    assert client.get(base, headers=auth).json()["activity"]["qrUrl"].endswith("/p/" + a["id"])

    cfg = {
        "type_options": ["嘉宾", "观众"],
        "department": {"enabled": True, "required": False},
        "unit": {"enabled": False, "required": False},
        "remark": {"enabled": True, "required": False},
    }
    assert client.put(f"{base}/fields", json=cfg, headers=auth).status_code == 200
    assert client.get(f"{base}/fields", headers=auth).json()["fieldsConfig"]["type_options"] == ["嘉宾", "观众"]

    client.post(f"/api/activity/{a['id']}/register", json={
        "phone": "13800138000", "name": "张三", "category": "嘉宾",
    })
    s = client.get(f"{base}/stats", headers=auth).json()["stats"]
    assert s["regCount"] == 1 and "serverTime" in s
    assert client.get(f"{base}/details?search=13800138000&status=pending&order=desc", headers=auth).json()["items"]
    assert client.get(f"{base}/export?format=xlsx", headers=auth).content[:2] == b"PK"
    assert client.get(f"{base}/export?format=csv", headers=auth).status_code == 200
    assert client.get(f"{base}/qrcode?size=10&format=png", headers=auth).content[:4] == b"\x89PNG"
    assert client.get(f"{base}/qrcode?format=svg", headers=auth).status_code == 200
    assert client.get(f"{base}/whitelist", headers=auth).status_code == 200
    assert client.put(f"{base}/whitelist", json={"items": []}, headers=auth).status_code == 200

    assert client.delete(base, headers=auth).status_code == 200


def test_admin_behavior_harness():
    node = _node()
    if not node:
        pytest.skip("node 不可用，跳过后台行为 harness")
    r = subprocess.run([node, str(HARNESS)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, "后台行为验证失败:\n" + r.stdout + "\n" + r.stderr
    assert "ADMIN_HARNESS_OK" in r.stdout
