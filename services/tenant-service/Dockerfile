# Build from the REPO ROOT (it needs libs/taraz-auth too):
#   docker build -f services/tenant-service/Dockerfile -t tenant-service .
# The base image is pinned by digest and every package by hash (requirements.lock, written by lock.py), so a
# build installs exactly what was reviewed. Dependabot proposes updates to both.
FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY services/tenant-service/requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes --no-deps -r /tmp/requirements.lock

COPY libs/taraz-auth /src/taraz-auth
COPY services/tenant-service /app
# Only the two packages themselves: their dependencies, and the build backend, came from the lock
RUN pip install --no-deps --no-build-isolation /src/taraz-auth /app \
 && pip check \
 && useradd --uid 10001 --create-home app

USER app
EXPOSE 8080
# Migrations run before each deploy (Railway pre-deploy / a Kubernetes Job): alembic upgrade head
# FORWARDED_ALLOW_IPS: the proxy addresses whose X-Forwarded-* headers are trusted. Set it to the proxy's range
# where it's known (Kubernetes); Railway's edge has no fixed range, hence the default.
CMD ["sh", "-c", "exec uvicorn tenant_service.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-*}\""]
