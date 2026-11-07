"""M4 部署适配验证：文件契约、方言中立性、环境变量覆盖度、preflight 自检逻辑。"""
from __future__ import annotations

import importlib.util
import re
import sys

from app.config import BASE_DIR


def _load_preflight():
    path = BASE_DIR / "scripts" / "preflight.py"
    spec = importlib.util.spec_from_file_location("qr4events_preflight", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Python 3.13 的 dataclasses 解析类型注解时会查 sys.modules；动态加载含
    # @dataclass 的模块必须先注册再 exec，否则抛 AttributeError 导致收集中断。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PF = _load_preflight()


def _good_env() -> dict[str, str]:
    return {
        "PUBLIC_BASE_URL": "https://qr.example.com",
        "APP_TZ": "Asia/Shanghai",
        "TZ": "Asia/Shanghai",
        "SECRET_KEY": "a-long-random-secret",
        "ADMIN_PASS": "a-strong-password",
        "DATABASE_URL": "postgresql+psycopg2://u:p@h:5432/db",
        "SERVE_STATIC": "1",
        "TRUST_PROXY": "0",
    }


def _codes(issues, level: str) -> set[str]:
    return {i.code for i in issues if i.level == level}


def test_deploy_files_exist():
    for name in ("Dockerfile", "scf_bootstrap", "部署与上线指南.md",
                 "scripts/preflight.py", "scripts/e2e_smoke.py"):
        assert (BASE_DIR / name).is_file(), f"缺少 {name}"


def test_container_adapter_contract():
    docker = (BASE_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "TZ=Asia/Shanghai" in docker, "容器必须固定时区"
    # 只看 CMD 行：Dockerfile 注释里写了「不要加 --proxy-headers」，不能误伤。
    cmd = "\n".join(ln for ln in docker.splitlines() if ln.strip().startswith("CMD"))
    assert cmd, "Dockerfile 必须包含 CMD"
    # Docker CMD 常用 exec 格式（"--port", "8000"），不能只按连续字符串匹配。
    assert re.search(r"--port[\"'\s,]+8000", cmd), "容器 CMD 必须监听 8000"
    # 安全：不能让 uvicorn 用可伪造的 XFF 覆盖 request.client.host
    assert "--proxy-headers" not in cmd, "开启 proxy-headers 会绕过 TRUST_PROXY 限流保护"

    boot = (BASE_DIR / "scf_bootstrap").read_text(encoding="utf-8")
    assert "--port 9000" in boot, "CloudBase HTTP 云函数必须监听 9000"
    assert "Asia/Shanghai" in boot
    assert "third_party" in boot, "云函数依赖需打包到 third_party/"
    assert "uvicorn app.main:app" in boot


def test_models_are_dialect_neutral():
    """本地 SQLite 验证与生产 PostgreSQL 共用同一套模型，不得含方言专用类型。"""
    models = (BASE_DIR / "app" / "models.py").read_text(encoding="utf-8")
    # 只匹配真正的类型使用 DateTime(，避免误伤 docstring 里「而非 DB DateTime」的说明文字。
    assert not re.search(r"\bDateTime\s*\(", models), "时间必须存 ISO 字符串，避免 SQLite/PG 时区语义差异"
    assert "JSON" in models
    assert "psycopg2-binary" in (BASE_DIR / "requirements.txt").read_text(encoding="utf-8")


def test_env_example_covers_every_setting_key():
    """config.py 读取的每个环境变量都必须在 .env.example 中有说明，防止文档脱节。"""
    cfg = (BASE_DIR / "app" / "config.py").read_text(encoding="utf-8")
    keys = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', cfg))
    keys |= set(re.findall(r'_bool\("([A-Z_]+)"', cfg))
    keys |= set(re.findall(r'_int\("([A-Z_]+)"', cfg))
    sample = (BASE_DIR / ".env.example").read_text(encoding="utf-8")
    declared = set(re.findall(r"^([A-Z_]+)=", sample, re.MULTILINE))
    missing = keys - declared
    assert not missing, f".env.example 缺少变量: {sorted(missing)}"


def test_preflight_clean_env_has_no_fail():
    issues = PF.run_checks(_good_env(), BASE_DIR)
    assert not _codes(issues, "FAIL"), [i.message for i in issues if i.level == "FAIL"]


def test_preflight_rejects_nonstandard_port_and_http():
    env = _good_env()
    env["PUBLIC_BASE_URL"] = "http://qr.example.com:8443/p"
    assert "PUBLIC_BASE_URL" in _codes(PF.run_checks(env, BASE_DIR), "FAIL")

    env["PUBLIC_BASE_URL"] = "https://qr.example.com:8443"
    assert "PUBLIC_BASE_URL" in _codes(PF.run_checks(env, BASE_DIR), "FAIL")

    env["PUBLIC_BASE_URL"] = "https://qr.example.com/base"
    assert "PUBLIC_BASE_URL" in _codes(PF.run_checks(env, BASE_DIR), "FAIL")


def test_preflight_rejects_wrong_timezone():
    env = _good_env()
    env["APP_TZ"] = "UTC"
    assert "APP_TZ" in _codes(PF.run_checks(env, BASE_DIR), "FAIL")


def test_preflight_requires_explicit_secret_and_warns_sqlite():
    env = _good_env()
    env["SECRET_KEY"] = ""
    assert "SECRET_KEY" in _codes(PF.run_checks(env, BASE_DIR), "FAIL")

    env = _good_env()
    env["DATABASE_URL"] = "sqlite:///./data/app.db"
    assert "DATABASE_URL" in _codes(PF.run_checks(env, BASE_DIR), "WARN")

    env["ADMIN_PASS"] = ""
    assert "ADMIN_PASS" in _codes(PF.run_checks(env, BASE_DIR), "WARN")


def test_preflight_detects_missing_static_files(tmp_path):
    issues = PF.run_checks(_good_env(), tmp_path)
    assert "STATIC" in _codes(issues, "FAIL")


def test_preflight_module_registered_for_dataclass():
    """动态加载含 @dataclass 的模块必须先注册 sys.modules，否则 Py3.13 收集中断。"""
    assert "qr4events_preflight" in sys.modules


def test_e2e_smoke_bypasses_environment_proxy():
    """回环请求被环境 HTTP_PROXY 劫持曾导致全部 404，必须保持绕过代理。"""
    src = (BASE_DIR / "scripts" / "e2e_smoke.py").read_text(encoding="utf-8")
    assert "trust_env=False" in src
    assert "HTTP_PROXY" in src and "NO_PROXY" in src


def test_bootstrap_is_path_independent():
    boot = (BASE_DIR / "scf_bootstrap").read_text(encoding="utf-8")
    assert "BASH_SOURCE" in boot, "必须用脚本自身目录定位代码/依赖"
    assert "command -v python3" in boot, "需兼容 python3/python 命名差异"
    assert "cd /app" not in boot, "不得硬编码平台解压路径 /app"


def test_preflight_does_not_misjudge_localhost_substring():
    env = _good_env()
    env["PUBLIC_BASE_URL"] = "https://localhost.example.com"
    issues = PF.run_checks(env, BASE_DIR)
    assert "PUBLIC_BASE_URL" not in _codes(issues, "WARN")
    assert "PUBLIC_BASE_URL" not in _codes(issues, "FAIL")


def test_preflight_deploy_checks_both_adapters(tmp_path):
    """DEPLOY 需兼容 exec/shell 两种 CMD 写法，并同时校验两侧时区。"""
    static = tmp_path / "app" / "static"
    static.mkdir(parents=True)
    for name in PF.STATIC_REQUIRED:
        (static / name).write_text("x", encoding="utf-8")
    (static / "filing.js").write_text("// configured", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("psycopg2-binary>=2.9", encoding="utf-8")
    boot = tmp_path / "scf_bootstrap"
    docker = tmp_path / "Dockerfile"
    boot.write_text("export TZ=Asia/Shanghai\nuvicorn app.main:app --port 9000\n",
                    encoding="utf-8")

    docker.write_text(
        'ENV TZ=Asia/Shanghai\nCMD ["uvicorn", "app.main:app", "--port", "8000"]\n',
        encoding="utf-8")
    assert "DEPLOY" not in _codes(PF.run_checks(_good_env(), tmp_path), "FAIL")

    docker.write_text("ENV TZ=Asia/Shanghai\nCMD uvicorn app.main:app --port 8000\n",
                      encoding="utf-8")
    assert "DEPLOY" not in _codes(PF.run_checks(_good_env(), tmp_path), "FAIL")

    docker.write_text('CMD ["uvicorn", "--port", "8000"]\n', encoding="utf-8")
    assert "DEPLOY" in _codes(PF.run_checks(_good_env(), tmp_path), "FAIL")
