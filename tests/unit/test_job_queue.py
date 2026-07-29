# tests/unit/test_job_queue.py
"""Job queue producer — rq.Queue construction (orchestrator/job_queue.py).

Queue()/Redis.from_url() perform no network I/O at construction time (rq
lazily connects on first enqueue), so this exercises the real objects rather
than mocking away the thing under test.
"""

import redis
from rq import Queue

from scraper_engine.orchestrator.job_queue import QUEUE_NAME, build_queue


class TestBuildQueue:
    def test_returns_queue_with_expected_name(self) -> None:
        queue = build_queue("redis://localhost:6379/0")
        assert isinstance(queue, Queue)
        assert queue.name == QUEUE_NAME

    def test_connection_is_redis_client_from_given_url(self) -> None:
        queue = build_queue("redis://localhost:6379/2")
        assert isinstance(queue.connection, redis.Redis)
        pool_kwargs = queue.connection.connection_pool.connection_kwargs
        assert pool_kwargs["host"] == "localhost"
        assert pool_kwargs["port"] == 6379
        assert pool_kwargs["db"] == 2
