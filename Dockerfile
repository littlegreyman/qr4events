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
RUN mkdir -p /app/data

EXPOSE 8000

# 注意：不要加 --proxy-headers。uvicorn 开启后会用客户端可伪造的
# X-Forwarded-For 覆盖 request.client.host，绕过 app/security.py 的
# TRUST_PROXY 保护，使接口限流形同虚设。若确实部署在可信反向代理之后，
# 请显式配置 --forwarded-allow-ips <反代IP> 并设置 TRUST_PROXY=1。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
