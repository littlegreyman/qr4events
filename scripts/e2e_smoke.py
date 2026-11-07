#!/usr/bin/env python3
"""端到端冒烟验证：启动真实 uvicorn，按平台中立通用码完整链路发真实 HTTP 请求。

与 pytest/TestClient（进程内 ASGI）不同，本脚本验证真实 ASGI 服务器下的：
同源静态托管、/p/{id} 路由、安全响应头、鉴权、报名幂等、导出与二维码二进制。

注意：CI/开发机常设置 HTTP_PROXY 且无 NO_PROXY，httpx 默认 trust_env=True 会把
127.0.0.1 的回环请求转发给代理并返回 404。因此这里显式 trust_env=False，并在
子进程环境里清除代理变量，保证请求直达本地 uvicorn。

用法：python scripts/e2e_smoke.py  （临时库与端口自动清理，退出码 0 表示全通过）
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Shanghai")


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, bool(ok), detail))
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self) -> bool:
        return bool(self.rows) and all(row[1] for row in self.rows)

    @property
    def failed(self) -> int:
        return sum(1 for row in self.rows if not row[1])


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_ready(client: httpx.Client, proc: subprocess.Popen) -> bool:
    for _ in range(80):
        if proc.poll() is not None:
            return False
        try:
            if client.get("/healthz").status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    return False


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_flow(client: httpx.Client, report: Report) -> None:
    health = client.get("/healthz")
    try:
        hb = health.json()
    except ValueError:
        hb = {}
    report.add(
        "健康检查（TZ=Asia/Shanghai）",
        health.status_code == 200 and hb.get("tz") == "Asia/Shanghai"
        and "+08:00" in str(hb.get("serverTime", "")),
        f"HTTP {health.status_code}, tz={hb.get('tz')}",
    )

    for path in ("/", "/privacy", "/admin", "/p/abc123"):
        r = client.get(path)
        report.add(f"页面 {path} 同源可达", r.status_code == 200 and "<html" in r.text.lower(),
                   f"HTTP {r.status_code}")

    for name in ("app.css", "p.js", "admin.js", "filing.js"):
        r = client.get("/static/" + name)
        report.add(f"静态资源 /static/{name}", r.status_code == 200, f"HTTP {r.status_code}")

    root = client.get("/")
    report.add(
        "安全响应头",
        root.headers.get("x-frame-options") == "DENY"
        and root.headers.get("x-content-type-options") == "nosniff",
        "X-Frame-Options / X-Content-Type-Options",
    )

    login = client.post("/api/admin/login",
                        json={"username": "admin", "password": "E2EAdmin!2026"})
    login_ok = login.status_code == 200 and "token" in login.json()
    report.add("管理员登录", login_ok, f"HTTP {login.status_code}")
    if not login_ok:
        return
    auth = {"Authorization": "Bearer " + login.json()["token"]}

    now = datetime.now(UTC).astimezone(TZ)

    def iso(hours: float) -> str:
        return (now + timedelta(hours=hours)).isoformat(timespec="seconds")

    payload = {
        "name": "端到端验证活动",
        "description": "真实 uvicorn 全流程",
        "location": "测试会场",
        "reg_start": iso(-1), "reg_end": iso(1),
        "checkin_start": iso(2), "checkin_end": iso(4),
        "status": "active", "reg_mode": "open",
        "allow_onsite_combo": True, "reg_enabled": True, "checkin_enabled": True,
    }
    created = client.post("/api/admin/activities", json=payload, headers=auth)
    create_ok = created.status_code == 200
    report.add("创建活动", create_ok, f"HTTP {created.status_code}")
    if not create_ok:
        return
    act = created.json()["activity"]
    aid = act["id"]
    report.add("二维码地址（https + 443 + 根路径 + /p/{id}）",
               act["qrUrl"] == f"https://qr.example.com/p/{aid}", act["qrUrl"])

    st = client.post(f"/api/activity/{aid}/status", json={})
    report.add("报名期状态查询 → form",
               st.status_code == 200 and st.json().get("action") == "form",
               f"action={st.json().get('action')}")

    reg = client.post(f"/api/activity/{aid}/register",
                      json={"phone": "13800138000", "name": "张三", "category": "普通"})
    rj = reg.json()
    report.add(
        "报名成功且手机号脱敏",
        reg.status_code == 200 and rj.get("status") == "registered"
        and "13800138000" not in reg.text
        and rj["registration"]["phone"] == "138****8000",
        f"status={rj.get('status')}",
    )
    report.add("凭证时间来自服务端 serverTime",
               "+08:00" in str(rj.get("serverTime", "")), str(rj.get("serverTime", "")))

    dup = client.post(f"/api/activity/{aid}/register",
                      json={"phone": "13800138000", "name": "张三", "category": "普通"})
    report.add("重复提交幂等", dup.json().get("status") == "already_registered",
               f"status={dup.json().get('status')}")

    st2 = client.post(f"/api/activity/{aid}/status", json={"phone": "13800138000"})
    report.add("已报名身份识别", st2.json().get("userState") == "registered",
               f"userState={st2.json().get('userState')}")

    stats = client.get(f"/api/admin/activities/{aid}/stats", headers=auth)
    report.add("看板统计", stats.status_code == 200
               and stats.json()["stats"]["regCount"] == 1, f"HTTP {stats.status_code}")

    det = client.get(f"/api/admin/activities/{aid}/details", headers=auth)
    items = det.json().get("items", [])
    report.add("明细含完整手机号（仅鉴权后）",
               len(items) == 1 and items[0].get("phoneFull") == "13800138000",
               f"{len(items)} 条")

    unauth = client.get(f"/api/admin/activities/{aid}/details")
    report.add("未鉴权访问明细被拒", unauth.status_code == 401, f"HTTP {unauth.status_code}")

    xlsx = client.get(f"/api/admin/activities/{aid}/export", headers=auth)
    report.add("导出 xlsx", xlsx.status_code == 200 and xlsx.content[:2] == b"PK",
               f"{len(xlsx.content)} bytes")

    qr = client.get(f"/api/admin/activities/{aid}/qrcode", headers=auth)
    report.add("二维码 PNG", qr.status_code == 200
               and qr.content[:8] == b"\x89PNG\r\n\x1a\n", f"{len(qr.content)} bytes")

    p = client.get(f"/p/{aid}")
    report.add(f"参与者页 /p/{aid} 200", p.status_code == 200, f"HTTP {p.status_code}")


def main() -> int:
    port = free_port()
    tmpdir = tempfile.mkdtemp(prefix="qr4events_e2e_")
    db_path = Path(tmpdir) / "e2e.db"
    log_path = Path(tmpdir) / "uvicorn.log"
    env = os.environ.copy()
    # 回环请求必须直达本地服务：清除代理变量并补 NO_PROXY 双保险。
    for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    env.update({
        "DATABASE_URL": f"sqlite:///{db_path}",
        "SECRET_KEY": "qr4events-e2e-fixed-secret-key-v1",
        "ADMIN_USER": "admin",
        "ADMIN_PASS": "E2EAdmin!2026",
        "PUBLIC_BASE_URL": "https://qr.example.com",
        "APP_TZ": "Asia/Shanghai",
        "TZ": "Asia/Shanghai",
        "SERVE_STATIC": "1",
        "RATE_LIMIT_GENERAL": "1000",
        "RATE_LIMIT_SUBMIT": "1000",
        "RATE_LIMIT_LOGIN": "1000",
    })
    cmd = [sys.executable, "-m", "uvicorn", "app.main:app",
           "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"]
    report = Report()
    try:
        with log_path.open("w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                                    stdout=log_file, stderr=subprocess.STDOUT)
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0,
                                  trust_env=False) as client:
                    ready = wait_ready(client, proc)
                    report.add("真实 uvicorn 服务启动", ready, f"127.0.0.1:{port}")
                    if ready:
                        run_flow(client, report)
            finally:
                stop(proc)
    except Exception as exc:  # 冒烟脚本把异常转成一条失败报告，便于 CI 判读
        report.add("端到端流程无异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        if not report.ok and log_path.is_file():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            print("\n--- uvicorn 日志（末尾 40 行）---")
            print("\n".join(lines[-40:]) or "(空)")
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n汇总：{len(report.rows) - report.failed}/{len(report.rows)} 项通过")
    print("结论：" + ("端到端验证全部通过" if report.ok else "存在失败项，请检查上方输出"))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
