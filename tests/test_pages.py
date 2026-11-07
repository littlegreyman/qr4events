"""M2 参与者 H5 全面验证。

覆盖：页面路由、静态资源完整性、DOM id 契约、前后端 action/API 契约对齐、
零第三方依赖、零跳转、无 innerHTML、客户端校验、备案号悬挂，以及
Node DOM 桩对 p.js 的真实行为验证。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from app.config import BASE_DIR

STATIC = BASE_DIR / "app" / "static"
HARNESS = BASE_DIR / "tests" / "frontend_harness.cjs"
HTML_PAGES = ("index.html", "participant.html", "privacy.html", "admin.html")
JS_FILES = ("p.js", "filing.js", "admin.js")
PAGE_ROUTES = ("/", "/p/abc123", "/admin", "/privacy")


def _node() -> str | None:
    return os.environ.get("NODE_BIN") or shutil.which("node")


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 路由与资源


@pytest.mark.parametrize("path", PAGE_ROUTES)
def test_page_routes_served(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    assert "text/html" in r.headers.get("content-type", "")
    assert "<html" in r.text.lower()


def test_static_assets_served(client):
    for name in ("app.css", "p.js", "filing.js", "participant.html",
                 "privacy.html", "index.html", "admin.html"):
        assert (STATIC / name).is_file(), f"缺少静态文件 {name}"
        r = client.get("/static/" + name)
        assert r.status_code == 200, f"/static/{name} -> {r.status_code}"


def test_all_local_refs_resolve(client):
    """页面里所有 /static/* 引用都必须真实存在且可访问（杜绝 404 脚本）。"""
    ref = re.compile(r'(?:src|href)="(/static/[^"#?]+)"')
    for name in HTML_PAGES:
        for url in ref.findall(_read(name)):
            assert (STATIC / url[len("/static/"):]).is_file(), f"{name} 引用了不存在的 {url}"
            assert client.get(url).status_code == 200, f"{name} 的 {url} 不可访问"


def test_participant_dom_ids_complete():
    """p.js 里 $('#id') 引用的节点必须都在 participant.html 中定义。"""
    js = _read("p.js")
    html = _read("participant.html")
    wanted = set(re.findall(r"\$\('#([\w-]+)'\)", js))
    present = set(re.findall(r'id="([\w-]+)"', html))
    missing = wanted - present
    assert not missing, f"participant.html 缺少 p.js 需要的节点: {sorted(missing)}"


def test_filing_line_present_on_all_pages():
    """ICP 备案号须全站悬挂，filing.js 的宿主节点不能缺失。"""
    for name in HTML_PAGES:
        assert 'id="filing-line"' in _read(name), f"{name} 缺少备案号挂载点"
        assert "/static/filing.js" in _read(name), f"{name} 未引入 filing.js"


# ---------------------------------------------------------------- 前后端契约对齐


def test_action_branches_cover_backend_constants():
    """p.js 的 switch 分支必须与 stage.py 的 ACTION_* 常量完全一致。"""
    cases = set(re.findall(r"case '([a-z_]+)':", _read("p.js")))
    stage_src = (BASE_DIR / "app" / "stage.py").read_text(encoding="utf-8")
    consts = set(re.findall(r'^ACTION_\w+ = "([a-z_]+)"', stage_src, re.MULTILINE))
    assert cases == consts, f"前端分支 {sorted(cases)} 与后端动作 {sorted(consts)} 不一致"


def test_js_api_paths_exist_in_routes():
    js = _read("p.js")
    main = (BASE_DIR / "app" / "main.py").read_text(encoding="utf-8")
    paths = set(re.findall(r"api\('([a-z_]+)'\)", js))
    assert paths == {"status", "register", "checkin"}
    for p in paths:
        assert f'/api/activity/{{activity_id}}/{p}"' in main, f"后端缺少 {p} 路由"


def test_all_runtime_actions_have_frontend_branch(client, create_activity):
    """按不同时间窗/开关实际请求 status，返回的 action 都必须有前端渲染分支。"""
    js = _read("p.js")
    cases = set(re.findall(r"case '([a-z_]+)':", js))
    seen = set()

    a = create_activity(name="报名期")
    seen.add(client.post(f"/api/activity/{a['id']}/status", json={}).json()["action"])

    b = create_activity(name="报到期", reg_start=-5, reg_end=-3,
                        checkin_start=-1, checkin_end=1)
    seen.add(client.post(f"/api/activity/{b['id']}/status", json={}).json()["action"])

    c = create_activity(name="未开始", reg_start=2, reg_end=3,
                        checkin_start=4, checkin_end=5)
    seen.add(client.post(f"/api/activity/{c['id']}/status", json={}).json()["action"])

    d = create_activity(name="间隙", reg_start=-5, reg_end=-3,
                        checkin_start=2, checkin_end=4)
    seen.add(client.post(f"/api/activity/{d['id']}/status", json={}).json()["action"])

    e = create_activity(name="报名关闭", reg_enabled=False)
    seen.add(client.post(f"/api/activity/{e['id']}/status", json={}).json()["action"])

    assert seen <= cases, f"出现前端无法渲染的 action: {sorted(seen - cases)}"
    assert {"form", "combo_form", "info", "message"} <= seen


# ---------------------------------------------------------------- 平台中立 / 安全约束


def test_no_external_assets_in_pages():
    for name in HTML_PAGES:
        text = _read(name)
        for m in re.finditer(r'<(?:script|link|img)[^>]*?(?:src|href)="([^"]+)"', text, re.IGNORECASE):
            url = m.group(1)
            assert not url.startswith(("http://", "https://", "//")), \
                f"{name} 引用了外部资源 {url}（违反零第三方 SDK 约束）"
        for bad in ("cdn.", "unpkg", "jsdelivr", "googleapis", "doubleclick"):
            assert bad not in text, f"{name} 含第三方域名 {bad}"


def test_no_redirect_and_no_xss_sinks_in_js():
    for name in JS_FILES:
        if not (STATIC / name).is_file():
            continue
        text = _read(name)
        for bad in ("location.href =", "location.replace", "location.assign",
                    "window.open(", "document.write", "innerHTML", "outerHTML",
                    "eval(", "new Function("):
            assert bad not in text, f"{name} 含危险调用 {bad}"


def test_client_side_guards_present():
    js = _read("p.js")
    assert "PHONE_RE" in js and "chk.checked" in js and "隐私政策" in js
    assert "localStorage" in js and "flushQueue" in js, "缺少弱网暂存/重放"
    assert "ne.network" in js, "缺少网络异常识别"


def test_filing_js_config_contract():
    js = _read("filing.js")
    assert "beian.miit.gov.cn" in js and "beian.gov.cn" in js
    assert "if (!cfg.icp && !cfg.police) return;" in js, "未备案时不得渲染占位备案号"
    assert "window.QR4EVENTS_FILING" in js


# ---------------------------------------------------------------- JS 语法与行为


def test_js_files_pass_node_check():
    node = _node()
    if not node:
        pytest.skip("node 不可用，跳过 JS 语法检查")
    for name in JS_FILES:
        p = STATIC / name
        if not p.is_file():
            continue
        r = subprocess.run([node, "--check", str(p)],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{name} 语法错误: {r.stderr}"


def test_frontend_behavior_harness():
    node = _node()
    if not node:
        pytest.skip("node 不可用，跳过前端行为 harness")
    r = subprocess.run([node, str(HARNESS)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, "前端行为验证失败:\n" + r.stdout + "\n" + r.stderr
    assert "FRONTEND_HARNESS_OK" in r.stdout
