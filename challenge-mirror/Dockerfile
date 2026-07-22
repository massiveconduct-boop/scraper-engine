FROM python:3.12-slim

WORKDIR /app

# Only external dependency is itsdangerous (signed cookie tokens); everything else
# is stdlib. Kept minimal on purpose — this is a test fixture, not a production service.
RUN pip install --no-cache-dir itsdangerous==2.2.0

COPY app/ ./app/

ENV CHALLENGE_MIRROR_SECRET_KEY=""
# In CI/staging, override via: docker run -e CHALLENGE_MIRROR_SECRET_KEY=$(openssl rand -hex 32)

EXPOSE 8090

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=2)" || exit 1

CMD ["python", "-m", "app.server"]
