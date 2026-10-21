# Build from the REPO ROOT (it needs libs/taraz-auth too):
#   docker build -f services/tenant-service/Dockerfile -t tenant-service .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY libs/taraz-auth /src/taraz-auth
COPY services/tenant-service /app
RUN pip install "/src/taraz-auth[fastapi]" /app \
 && useradd --uid 10001 --create-home app

USER app
EXPOSE 8080
# Migrations run before each deploy (Railway pre-deploy / a Kubernetes Job): alembic upgrade head
CMD ["sh", "-c", "exec uvicorn tenant_service.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
