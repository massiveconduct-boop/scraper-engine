# orchestrator/job_queue.py
"""Job queue producer — the sync rq.Queue used to enqueue scrape/crawl jobs.

rq's producer API (``Queue.enqueue``) is synchronous, so this uses a plain
sync redis-py connection, separate from the app's async ``RedisClient``
(storage/redis_client.py) which is purpose-built for tenant-scoped async
reads/writes. Both point at the same Redis instance.

All workers (docker-compose.yml worker-l1/l2/l3, all bound to ``rq worker
scraper-jobs``) pull from this one queue — Worker.process_job already runs
the full L1->L2->L3 escalation internally per URL, so one queue with N
worker processes gives horizontal concurrency without needing per-level
queues.
"""

from __future__ import annotations

import redis
from rq import Queue

QUEUE_NAME = "scraper-jobs"


def build_queue(redis_url: str) -> Queue:
    """Construct the rq Queue producer handle for the given Redis URL."""
    connection = redis.Redis.from_url(redis_url)
    return Queue(QUEUE_NAME, connection=connection)
