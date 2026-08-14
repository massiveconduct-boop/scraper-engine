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
#
# chromium is REQUIRED for Botasaurus: fetcher/botasaurus_wrapper.py's
# @browser-decorated fetch is L2's configured first attempt
# (config.levels.level_2.engine default "botasaurus+camoufox"), but
# botasaurus_driver drives a real Chrome/Chromium binary — it does not bundle
# one itself the way Playwright/Camoufox bundle Firefox. Without a browser
# installed, botasaurus_driver.core.config.find_chrome_executable() raises
# FileNotFoundError on every single fetch, immediately and silently (caught
# by botasaurus_wrapper.py's broad except → falls back to Camoufox every
# time, so this was never visibly failing — L2 still worked via the
# fallback, just always skipping its configured first attempt). Google
# Chrome itself ships no Linux aarch64 build at all; chromium (open-source,
# has real aarch64 packages) is what botasaurus_driver's own
# get_linux_executable_path() searches for as a named fallback — confirmed
# against the installed package, no code change needed, just the binary.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates xvfb chromium \
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
# requirements-dev-lock.txt (round 28) is the single source of pinned
# versions, shared with CI — rather than `pip install -e .` (needs the
# source tree present, defeating the point of installing deps before
# copying source) or a 3rd hand-duplicated package list (the drift class
# that caused the round-27 types-redis mismatch, operations.md #12).
# dev-lock (not the runtime-only lock) because migrations need
# alembic+sqlalchemy — those are dev-extras, not src/ runtime deps, but this
# container ships migrations/ too (COPY . . below). This stage's own CMD does
# NOT run migrations itself (bare uvicorn, see the runtime stage below) — the
# same image is reused, via a `command: alembic upgrade head` override, by
# docker-compose's one-shot `migrate` init service (see docker-compose.yml),
# which every Postgres-writing service depends on via
# `condition: service_completed_successfully` before it starts. Scope gap:
# this only covers `docker compose up` — a bare `docker run <image>` outside
# compose does not auto-migrate.
COPY requirements-dev-lock.txt .
RUN pip install --no-cache-dir "camoufox[geoip]" -r requirements-dev-lock.txt

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
# Registers the scraper_engine package into site-packages (--no-deps: deps
# are already installed above, this is just the local package itself) so
# `scraper_engine.*` resolves regardless of cwd — same reasoning as the
# editable install used in dev, without needing PYTHONPATH.
RUN pip install --no-cache-dir --no-deps .

ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production
EXPOSE 8000 9090
# supervisord runs api + the 3 self-healing daemons (proxy-harvester,
# dlq-reaper, webhook-sweeper) together as one container — see
# docker/supervisord.conf. worker-l1/l2/l3 and migrate override this CMD
# via their own `command:` in docker-compose.yml, so they're unaffected.
# Copied to supervisorctl's default config search path (rather than left
# under /app/docker) so `docker exec <container> supervisorctl status`
# works without an explicit -c flag.
RUN mkdir -p /etc/supervisor && cp docker/supervisord.conf /etc/supervisor/supervisord.conf
CMD ["supervisord", "-c", "/etc/supervisor/supervisord.conf"]
