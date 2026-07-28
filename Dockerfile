# Multi-stage build (round 13 E2).
#
# Layer order is deliberate: the rarely-changing Camoufox Firefox binary
# (~300MB, BD-02) and the dependency install live in an early, cache-stable
# stage; the frequently-changing application code is copied LAST. An app-only
# change (the common case) therefore reuses the cached Camoufox+deps layers
# instead of re-fetching the 300MB binary every build.
#
# NOTE ON SIZE: the Camoufox Firefox binary is unavoidable (~300MB) and is baked
# into the final image in BOTH the old and new layouts — this restructure is a
# BUILD-CACHE win (app changes skip the Camoufox re-fetch), not primarily an
# image-size win. See docs/round-13-evidence.md for the honest before/after.

# ── Stage 1: system deps shared by builder and runtime ──────────────────────
FROM python:3.12-slim AS system-base
# xvfb is REQUIRED: production config uses camoufox headless_mode=virtual, which
# launches Firefox inside a virtual X display. Without it, every L2/L3 browser
# fetch dies at runtime with `camoufox.exceptions.CannotFindXvfb`. (This was a
# latent gap in the pre-round-13 image too — surfaced by running the browser
# chaos suite inside the rebuilt image.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates xvfb \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libgtk-3-0 libx11-xcb1 \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 2a: Camoufox binary — bare base, isolated from system-dep changes ──
# On plain python:3.12-slim (NOT system-base) so that changing apt packages
# (e.g. adding xvfb) never invalidates this 300MB Firefox fetch. This is the
# single most expensive, least-frequently-changing layer.
FROM python:3.12-slim AS camoufox-fetch
# camoufox[geoip] — production config sets camoufox.geoip=true; the plain
# `camoufox` package raises NotInstalledGeoIPExtra at launch without the extra.
RUN pip install --no-cache-dir "camoufox[geoip]" && \
    python -m camoufox fetch || echo "Camoufox fetch skipped (binary may not be available)"

# ── Stage 2b: Python deps (cache-stable) ────────────────────────────────────
FROM system-base AS deps
WORKDIR /app
# Explicit runtime dependency list (mirrors .github/workflows/test.yml) rather
# than `pip install -e .` — an editable install needs the source tree present,
# which would defeat the whole point of installing deps before copying source.
# A container never needs the editable install; the app runs from COPY . . below.
RUN pip install --no-cache-dir "camoufox[geoip]" \
    fastapi uvicorn pydantic pydantic-core httpx scrapling \
    asyncpg redis rq botasaurus botasaurus-requests structlog prometheus-client \
    pyyaml boto3 python-dotenv alembic sqlalchemy scrapy maxminddb \
    opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc \
    opentelemetry-instrumentation-fastapi opentelemetry-instrumentation-httpx \
    opentelemetry-instrumentation-asyncpg opentelemetry-instrumentation-redis

# ── Stage 3: runtime — app code copied LAST ─────────────────────────────────
FROM system-base AS runtime
WORKDIR /app
# Cache-stable layers from the deps stage.
COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin
# Camoufox binary from its isolated stage. Stored under ~/.cache/camoufox (NOT
# /root/.camoufox — that stale path was a latent bug in the pre-round-13 Dockerfile).
COPY --from=camoufox-fetch /root/.cache/camoufox /root/.cache/camoufox
# Application code — the only layer that changes on a typical rebuild.
COPY . .

ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production
EXPOSE 8000 9090
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
