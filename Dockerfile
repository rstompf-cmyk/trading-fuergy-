# Trading Fuergy — FTV + batéria webová appka
# Multi-stage build: stage 1 = build deps (kompiluje sa SciPy/pandas), stage 2 = runtime + Chromium

# ============================================================
# Stage 1: build wheels
# ============================================================
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build dependencies pre SciPy, pandas, lxml, cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        gfortran \
        libffi-dev \
        libssl-dev \
        libxml2-dev \
        libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --user -r requirements.txt

# ============================================================
# Stage 2: runtime
# ============================================================
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH=/root/.local/bin:$PATH \
    # Defaultné konfigurácie (môžu byť prepísané cez docker-compose env)
    USE_DB=1 \
    AUTH_REQUIRED=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000

# Runtime deps:
# - libxml2/libxslt1 — runtime pre lxml
# - libssl3 — pre HTTPS / mTLS
# - libgomp1 — OpenMP pre SciPy/sklearn
# - curl — health check + debug
# - tini — proper signal handling (graceful shutdown)
# - tzdata — pre TZ env var (Europe/Bratislava) namiesto UTC defaultu
# - Playwright dependencies (Chromium headless) — bez nich SEPS/historian cookies refresh nepojde
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 \
        libxslt1.1 \
        libssl3 \
        libgomp1 \
        curl \
        tini \
        tzdata \
        # Playwright Chromium headless dependencies
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libdbus-1-3 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Bug S2 (2026-06-06): TZ Europe/Bratislava — symlink /etc/localtime + /etc/timezone
# Predtým bol len ENV TZ + tzdata package, ale Python datetime.now() vracal UTC
# lebo OS timezone nebol nastavený (žiadny symlink). Teraz Python a všetky systémové
# tools (datetime, log timestampy, cron) konečne pracujú s Europe/Bratislava.
ENV TZ=Europe/Bratislava
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone

# Skopíruj nainštalované Python balíky z builder stage
COPY --from=builder /root/.local /root/.local

# Stiahni Chromium pre Playwright (cache adresár /root/.cache/ms-playwright)
RUN python -m playwright install chromium

WORKDIR /app

# Skopíruj aplikačný kód
COPY . .

# Vytvor potrebné adresáre s default permissions
RUN mkdir -p /app/out /app/db/data /app/okte_credentials

EXPOSE 8000

# Health check — `/me` vracia JSON aj keď AUTH_REQUIRED=1 (je v public allowlist)
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fs http://localhost:${APP_PORT}/me || exit 1

# Tini pre proper SIGTERM handling (Python signal handlers fungujú správne)
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command — spustí appku cez Python entry point (app.py má vlastný uvicorn launcher)
CMD ["python", "app.py"]
