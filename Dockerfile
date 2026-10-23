# =============================================================================
# Production image for the LLM Metering Engine.
#
#   docker build -t llm-metering:latest .
#   docker run --rm -p 8000:8000 --env-file .env llm-metering:latest
#
# Multi-stage: the builder resolves the (heavy) scientific stack into a
# virtualenv, the runtime stage carries only that virtualenv. The runtime
# image therefore contains no build toolchain and no compiler.
# =============================================================================

# --- Stage 1: build the dependency environment ------------------------------
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# build-essential + libgomp are needed to build the scikit-learn / numpy
# wheels; they do not survive into the runtime stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy the metadata first so the dependency layer is cached independently of
# the application source.
COPY pyproject.toml README.md ./
COPY app/__init__.py ./app/__init__.py
COPY app/routers/__init__.py ./app/routers/__init__.py

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install .

# --- Stage 2: runtime --------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    UVICORN_WORKERS=2

# curl is used by the container health check.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin metering

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=metering:metering app/ ./app/
COPY --chown=metering:metering db/ ./db/
COPY --chown=metering:metering pyproject.toml README.md ./

USER metering

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
