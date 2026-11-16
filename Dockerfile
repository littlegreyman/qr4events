# qr4events · 平台中立通用码 · 同源部署镜像（C2：轻量服务器 / 任意容器平台）
# FastAPI 同时托管 /p/* 参与者页、/admin 后台、/api/* 接口、/static/* 资源。
FROM python:3.13-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /app/data \
    && chown -R app:app /app

USER app

EXPOSE 8000

# 不要开启 uvicorn --proxy-headers；应用按 TRUSTED_PROXY_IPS 校验直连代理，
# 仅对可信代理送来的 X-Forwarded-For 取值。边缘代理应覆盖/安全追加该头，
# 并通过网络策略禁止客户端绕过代理直连应用。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
