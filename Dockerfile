# syntax=docker/dockerfile:1

# The build context is the project root, so backend/ and frontend/ stay side by side
# exactly as main.py expects (FRONTEND_DIR = <backend>/../frontend).
ARG PYTHON_VERSION=3.12

# Stage 1: Poetry plus an empty virtualenv shared by the dependency stages.
FROM python:${PYTHON_VERSION}-slim AS poetry-base
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false
RUN pip install "poetry>=2.0,<3.0" \
    && python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"
WORKDIR /build
COPY backend/pyproject.toml backend/poetry.lock* ./

# Stage 2: runtime dependencies only.
FROM poetry-base AS deps-runtime
RUN poetry install --no-root --only main

# Stage 3: runtime plus development dependencies (httpx is required by the test client).
FROM poetry-base AS deps-test
RUN poetry install --no-root \
    && python -c "import httpx, pytest"

# Stage 4: test image. Build with: docker build --target test -t fleetroute-ocpi:test .
FROM python:${PYTHON_VERSION}-slim AS test
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"
COPY --from=deps-test /opt/venv /opt/venv
WORKDIR /app/backend
COPY backend/ ./
COPY frontend /app/frontend
# The Docker files are copied too so the tests can check them.
COPY Dockerfile docker-compose.yml .dockerignore /app/
CMD ["pytest", "-q"]

# Stage 5: runtime image (default target, so it must stay the last stage).
FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"
# Unprivileged user; /var/lib/ocpi is the shared folder for the simulator's live data file.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /var/lib/ocpi \
    && chown app:app /var/lib/ocpi
COPY --from=deps-runtime /opt/venv /opt/venv
WORKDIR /app/backend
COPY --chown=app:app backend/code ./code
COPY --chown=app:app backend/data ./data
COPY --chown=app:app backend/scripts ./scripts
COPY --chown=app:app frontend /app/frontend
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"]
# Single worker on purpose: reservations live in process memory.
CMD ["uvicorn", "main:app", "--app-dir", "code", "--host", "0.0.0.0", "--port", "8000"]