FROM python:3.11-slim
RUN pip install --no-cache-dir itsdangerous
COPY app/ ./app/
COPY test_challenge_mirror.py manual_verify.py ./
ENV CHALLENGE_MIRROR_SECRET_KEY=""
EXPOSE 8090
CMD ["python", "-m", "app.server"]
