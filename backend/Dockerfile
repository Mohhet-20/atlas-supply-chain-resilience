# ATLAS backend image. Not yet built or run: build and smoke-test it before relying on it (docs/deployment.md).
FROM python:3.10-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt

FROM python:3.10-slim AS runtime
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 ATLAS_ENVIRONMENT=production ATLAS_DATA_DIR=/data
RUN useradd --system --uid 10001 --home-dir /app atlas && mkdir -p /data && chown atlas /data
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=atlas alembic.ini ./
COPY --chown=atlas alembic ./alembic
COPY --chown=atlas app ./app
USER atlas
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2).status==200 else 1)"
# One API process: the cache and rate limiter are in-process (docs/architecture.md). Migrations are NOT run here: the
# compose "migrate" service (alembic upgrade head) runs them explicitly. The worker is this same image run as `python -m app.worker`.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
