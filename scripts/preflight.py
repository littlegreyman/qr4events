#!/usr/bin/env python3
"""部署前静态自检（只读，不启动服务、不修改任何文件）。

检查方案 C 的四条硬约束与 Serverless 特有陷阱：
  - PUBLIC_BASE_URL 必须 https + 标准 443 + 根路径、无跳转参数
  - TZ / APP_TZ 必须为 Asia/Shanghai（否则时间窗偏移 8 小时）
  - SECRET_KEY / ADMIN_PASS 必须显式设置（云函数无持久磁盘）
  - DATABASE_URL 生产应为 PostgreSQL；SQLite 仅本地验证
  - 静态页面/脚本齐全、备案号已填写
用法：
    python scripts/preflight.py             # FAIL 才返回非 0
    python scripts/preflight.py --strict    # WARN 也返回非 0
"""
from __future__ import annotations

import ipaddress
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent

STATIC_REQUIRED = (
    "index.html", "participant.html", "admin.html", "privacy.html",
    "app.css", "p.js", "admin.js", "filing.js",
)


@dataclass(frozen=True)
class Issue:
    level: str  # PASS / WARN / FAIL
    code: str
    message: str


def _load_env_file(path: Path, env: dict[str, str]) -> None:
    """极简 .env 解析，不依赖第三方库；不覆盖已有进程环境变量。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        env.setdefault(key.strip(), value.strip())


def check_public_base_url(value: str, issues: list[Issue]) -> None:
    code = "PUBLIC_BASE_URL"
    if not value:
        issues.append(Issue("FAIL", code, "未设置，二维码地址会退化为 localhost"))
        return
    parsed = urlparse(value)
    # 只按主机名判断回环地址，避免把 localhost.example.com 之类的合法域名误判。
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"} or host.startswith("127."):
        issues.append(Issue("WARN", code, "当前为 localhost/回环地址，仅供本地测试，不可上线"))
    if parsed.scheme != "https":
        issues.append(Issue("FAIL", code, "必须是 https（微信内 http 会被拦截）"))
    try:
        port = parsed.port
    except ValueError:
        issues.append(Issue("FAIL", code, "端口格式非法"))
        return
    if port not in (None, 443):
        issues.append(Issue("FAIL", code, f"必须是标准 443，当前为 {port}（8443 等非标端口是历史失败根因）"))
    if parsed.path not in ("", "/"):
        issues.append(Issue("FAIL", code, f"必须是根路径，不能带子路径 {parsed.path!r}"))
    if parsed.query or parsed.fragment or parsed.params:
        issues.append(Issue("FAIL", code, "不能带查询参数或片段（零跳转直达）"))
    if parsed.scheme == "https" and port in (None, 443) and parsed.path in ("", "/"):
        issues.append(Issue("PASS", code, f"符合 https + 443 + 根路径：{value}"))


def run_checks(env: dict[str, str], root: Path = ROOT) -> list[Issue]:
    issues: list[Issue] = []

    check_public_base_url(env.get("PUBLIC_BASE_URL", "").strip(), issues)

    tz = env.get("APP_TZ", "Asia/Shanghai").strip()
    if tz != "Asia/Shanghai":
        issues.append(Issue("FAIL", "APP_TZ", f"必须为 Asia/Shanghai，当前 {tz!r}（时间窗会偏移 8 小时）"))
    else:
        issues.append(Issue("PASS", "APP_TZ", "Asia/Shanghai"))

    sys_tz = env.get("TZ", "").strip()
    if sys_tz != "Asia/Shanghai":
        issues.append(Issue("WARN", "TZ", "容器 TZ 未设为 Asia/Shanghai；代码已显式取 UTC 转北京时间，但日志/系统时间会不一致"))
    else:
        issues.append(Issue("PASS", "TZ", "Asia/Shanghai"))

    if not env.get("SECRET_KEY", "").strip():
        issues.append(Issue("FAIL", "SECRET_KEY", "未显式设置；Serverless 无持久磁盘，重启后密钥丢失将导致历史手机号永久无法解密"))
    else:
        issues.append(Issue("PASS", "SECRET_KEY", "已显式设置"))

    if not env.get("ADMIN_PASS", "").strip():
        issues.append(Issue("WARN", "ADMIN_PASS", "未显式设置；Serverless 重启后自动生成的口令会丢失，建议显式设置"))
    else:
        issues.append(Issue("PASS", "ADMIN_PASS", "已显式设置"))

    db = env.get("DATABASE_URL", "").strip()
    if not db:
        issues.append(Issue("FAIL", "DATABASE_URL", "未设置"))
    elif db.startswith("sqlite"):
        issues.append(Issue("WARN", "DATABASE_URL", "SQLite 依赖本地磁盘，不适用于多副本 Serverless；生产请改 postgresql+psycopg2://"))
    elif db.startswith("postgresql"):
        issues.append(Issue("PASS", "DATABASE_URL", "PostgreSQL"))
    else:
        issues.append(Issue("WARN", "DATABASE_URL", f"未知协议 {db.split(':', 1)[0]!r}，请确认 SQLAlchemy 驱动已安装"))

    serve = env.get("SERVE_STATIC", "1").strip()
    if serve == "0":
        issues.append(Issue("WARN", "SERVE_STATIC", "静态托管分离需 rewrite /p/* 且会带来跨域/备案复杂度，方案 C 推荐同源 =1"))
    else:
        issues.append(Issue("PASS", "SERVE_STATIC", "同源托管（/p/*、/admin、/api/*、/static/* 同一域名）"))

    proxy = env.get("TRUST_PROXY", "0").strip()
    if proxy.lower() not in {"", "0", "1", "true", "false", "yes", "no", "on", "off"}:
        issues.append(Issue("WARN", "TRUST_PROXY", f"取值 {proxy!r} 非法，_bool() 会按 false 处理"))
    elif proxy.lower() in {"1", "true", "yes", "on"} and not env.get("TRUSTED_PROXY_IPS", "").strip():
        issues.append(Issue("FAIL", "TRUSTED_PROXY_IPS", "TRUST_PROXY=1 时必须配置可信代理 IP/CIDR"))
    else:
        issues.append(Issue("PASS", "TRUST_PROXY", f"={proxy}（代理信任由 TRUSTED_PROXY_IPS 限定）"))
    proxy_ips = env.get("TRUSTED_PROXY_IPS", "").strip()
    if proxy_ips:
        try:
            for item in proxy_ips.split(","):
                ipaddress.ip_network(item.strip(), strict=False)
        except ValueError:
            issues.append(Issue("FAIL", "TRUSTED_PROXY_IPS", "必须是逗号分隔的 IP 地址或 CIDR"))

    try:
        max_request_bytes = int(env.get("MAX_REQUEST_BYTES", str(2 * 1024 * 1024)))
        if not 1024 <= max_request_bytes <= 16 * 1024 * 1024:
            raise ValueError
    except ValueError:
        issues.append(Issue("FAIL", "MAX_REQUEST_BYTES", "必须是 1 KiB 到 16 MiB 之间的整数"))

    static_dir = root / "app" / "static"
    missing = [name for name in STATIC_REQUIRED if not (static_dir / name).is_file()]
    if missing:
        issues.append(Issue("FAIL", "STATIC", f"缺少静态文件：{', '.join(missing)}"))
    else:
        issues.append(Issue("PASS", "STATIC", f"{len(STATIC_REQUIRED)} 个页面/脚本齐全"))

    filing = (static_dir / "filing.js").read_text(encoding="utf-8") if (static_dir / "filing.js").is_file() else ""
    if "DEFAULT = { icp: '', police: '' }" in filing:
        issues.append(Issue("WARN", "FILING", "备案号尚未填写；备案通过后须在 filing.js 填入 ICP 号并在 30 日内完成公安联网备案"))
    elif not filing:
        issues.append(Issue("FAIL", "FILING", "filing.js 缺失"))
    else:
        issues.append(Issue("PASS", "FILING", "备案号已配置"))

    req = (root / "requirements.txt").read_text(encoding="utf-8") if (root / "requirements.txt").is_file() else ""
    if "psycopg2-binary" in req:
        issues.append(Issue("PASS", "PG_DRIVER", "PostgreSQL 驱动已声明"))
    else:
        issues.append(Issue("FAIL", "PG_DRIVER", "requirements.txt 缺少 psycopg2-binary"))

    bootstrap = (root / "scf_bootstrap").read_text(encoding="utf-8") if (root / "scf_bootstrap").is_file() else ""
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8") if (root / "Dockerfile").is_file() else ""
    # 兼容 Dockerfile 的 exec 格式（"--port", "8000"）与 shell 格式（--port 8000）。
    deploy_ok = (
        re.search(r"--port[\"'\s,]+9000", bootstrap) is not None
        and "Asia/Shanghai" in bootstrap
        and re.search(r"--port[\"'\s,]+8000", dockerfile) is not None
        and "Asia/Shanghai" in dockerfile
    )
    if deploy_ok:
        issues.append(Issue("PASS", "DEPLOY", "scf_bootstrap 监听 9000 且固定 TZ；Dockerfile 监听 8000 且固定 TZ"))
    else:
        issues.append(Issue("FAIL", "DEPLOY", "部署适配文件缺失，或端口/时区配置不正确"))

    return issues


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    strict = "--strict" in argv
    env = dict(os.environ)
    _load_env_file(ROOT / ".env", env)
    issues = run_checks(env, ROOT)

    for it in issues:
        print(f"[{it.level}] {it.code}: {it.message}")
    fails = [i for i in issues if i.level == "FAIL"]
    warns = [i for i in issues if i.level == "WARN"]
    passes = [i for i in issues if i.level == "PASS"]
    print(f"\n汇总：{len(fails)} FAIL / {len(warns)} WARN / {len(passes)} PASS")
    if fails:
        print("结论：未通过，禁止上线（先修复上述 FAIL）。")
        return 1
    if strict and warns:
        print("结论：--strict 模式下存在 WARN，未通过。")
        return 1
    print("结论：硬性约束全部通过；WARN 项为上线前需人工确认事项。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
