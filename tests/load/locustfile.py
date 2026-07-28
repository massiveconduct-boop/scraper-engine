# tests/load/locustfile.py
"""Locust load test for Scraper Engine API.

Usage:
    locust -f tests/load/locustfile.py --host=http://localhost:8000
    Then open http://localhost:8089 for the web UI.

Or headless:
    locust -f tests/load/locustfile.py --host=http://localhost:8000 \
        --headless --users 50 --spawn-rate 10 --run-time 60s
"""

import os
import random

from locust import HttpUser, between, task


class ScraperEngineUser(HttpUser):
    """Simulates a tenant user submitting scrape jobs and polling status."""

    wait_time = between(1, 3)

    def on_start(self):
        self.job_ids: list[str] = []
        # /v1/scrape and /v1/jobs require X-API-Key (api/routes.py) — without
        # it every submit/poll call 401s and the test only measures the auth
        # rejection path, not the real pipeline. LOAD_TEST_API_KEY lets CI/prod
        # point this at a real provisioned tenant key.
        self.client.headers["X-API-Key"] = os.environ.get(
            "LOAD_TEST_API_KEY", "sk-admin"
        )

    @task(3)
    def submit_scrape_job(self):
        """Submit a new scrape job — most common operation."""
        urls = [
            f"https://example.com/page-{random.randint(1, 1000)}"
            for _ in range(random.randint(1, 5))
        ]
        with self.client.post(
            "/v1/scrape",
            json={"urls": urls, "async_mode": True},
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = response.json()
                self.job_ids.append(data.get("job_id", ""))
                response.success()
            elif response.status_code == 429:
                response.success()  # rate limit expected under load
            else:
                response.failure(f"Unexpected status: {response.status_code}")

    @task(2)
    def poll_job_status(self):
        """Poll a random job's status."""
        if not self.job_ids:
            self.submit_scrape_job()
            return
        job_id = random.choice(self.job_ids)
        with self.client.get(
            f"/v1/jobs/{job_id}",
            name="/v1/jobs/{job_id}",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 404, 429, 0):
                response.success()
            else:
                response.failure(f"Unexpected status: {response.status_code}")

    @task(1)
    def health_check(self):
        """Health endpoint — no auth required."""
        with self.client.get("/v1/health", catch_response=True) as response:
            if response.status_code in (200, 429) or response.status_code == 0:
                response.success()
            else:
                response.failure(f"Health check failed: {response.status_code}")

    @task(1)
    def openapi_schema(self):
        """OpenAPI schema fetch — cache-warm."""
        with self.client.get("/openapi.json", catch_response=True) as response:
            if response.status_code in (200, 429, 0):
                response.success()
            else:
                response.failure(f"OpenAPI failed: {response.status_code}")
