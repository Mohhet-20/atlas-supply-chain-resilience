# ATLAS public demo API (synthetic data, rebuilt on every start). Build context is the repository root:
#   docker build -f deploy/demo/Dockerfile -t atlas-demo .
# Used by render.yaml. For a real deployment use backend/Dockerfile and docker-compose.yml instead.
FROM python:3.11-slim
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    ATLAS_ENVIRONMENT=staging ATLAS_DATA_DIR=/data ATLAS_DEMO_DATA_ENABLED=true ATLAS_EMBEDDED_WORKER=false \
    ATLAS_ALERTING_TICK_SECONDS=0
RUN useradd --system --uid 10001 --home-dir /app atlas && mkdir -p /data && chown atlas /data
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
# httpx: scripts/demo_world.py drives the real API through FastAPI's TestClient to build the demo world.
RUN pip install -r backend/requirements.txt httpx==0.28.1
COPY --chown=atlas backend/alembic.ini backend/alembic.ini
COPY --chown=atlas backend/alembic backend/alembic
COPY --chown=atlas backend/app backend/app
COPY --chown=atlas scripts/demo_world.py scripts/demo_start.py scripts/
COPY --chown=atlas data/demo data/demo
USER atlas
EXPOSE 8000
CMD ["python", "scripts/demo_start.py"]
