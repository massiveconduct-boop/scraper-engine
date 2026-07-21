# Multi-stage build — Camoufox Firefox binary baked into image (BD-02)
# ~250MB extra image size, instant container startup, no runtime download failures

FROM python:3.11-slim AS builder

WORKDIR /app

# Install system deps for Camoufox + Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    ca-certificates \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Copy full project
COPY . .
RUN pip install --no-cache-dir -e ".[dev]"

# Fetch Camoufox Firefox binary (BD-02: baked at build time)
RUN pip install --no-cache-dir camoufox && python -m camoufox fetch || echo "Camoufox fetch skipped (binary may not be available)"


FROM python:3.11-slim AS runtime

WORKDIR /app

# Copy system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages + Camoufox binary from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /root/.camoufox /root/.camoufox

# Copy application code
COPY . .

ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production

EXPOSE 8000 9090

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
