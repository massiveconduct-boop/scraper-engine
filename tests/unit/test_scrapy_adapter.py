# tests/unit/test_scrapy_adapter.py
"""ScrapyAdapter — rewritten to isolate each crawl in its own subprocess.

CrawlerProcess.start() can only run once per OS process (Twisted's reactor
can't restart); the original in-process implementation would crash every
crawl job after the first inside a long-lived rq worker. These tests mock
the subprocess boundary itself (multiprocessing.Process/Queue) rather than
spawning real Scrapy/Twisted, which would be slow and heavy for a unit test.
"""

from unittest.mock import MagicMock, patch

import pytest

from scraper_engine.services import scrapy_adapter as sa
from scraper_engine.services.scrapy_adapter import ScrapyAdapter


@pytest.mark.asyncio
async def test_run_spider_returns_items_from_subprocess():
    items = [{"url": "http://example.com", "title": "Example"}]

    fake_queue = MagicMock()
    fake_queue.get.return_value = items
    fake_process = MagicMock()

    fake_ctx = MagicMock()
    fake_ctx.Queue.return_value = fake_queue
    fake_ctx.Process.return_value = fake_process

    with patch("multiprocessing.get_context", return_value=fake_ctx):
        adapter = ScrapyAdapter()
        adapter._available = True
        result = await adapter.run_spider("titles", ["http://example.com"])

    assert result == items
    fake_process.start.assert_called_once()
    fake_process.join.assert_called()


@pytest.mark.asyncio
async def test_run_spider_returns_empty_list_when_scrapy_unavailable():
    adapter = ScrapyAdapter()
    adapter._available = False
    result = await adapter.run_spider("titles", ["http://example.com"])
    assert result == []


@pytest.mark.asyncio
async def test_run_spider_returns_empty_list_when_subprocess_raises():
    fake_queue = MagicMock()
    fake_queue.get.return_value = RuntimeError("spider crashed")
    fake_process = MagicMock()

    fake_ctx = MagicMock()
    fake_ctx.Queue.return_value = fake_queue
    fake_ctx.Process.return_value = fake_process

    with patch("multiprocessing.get_context", return_value=fake_ctx):
        adapter = ScrapyAdapter()
        adapter._available = True
        result = await adapter.run_spider("titles", ["http://example.com"])

    assert result == []


@pytest.mark.asyncio
async def test_run_spider_terminates_process_on_timeout():
    fake_queue = MagicMock()
    fake_process = MagicMock()

    fake_ctx = MagicMock()
    fake_ctx.Queue.return_value = fake_queue
    fake_ctx.Process.return_value = fake_process

    async def _raise_timeout(awaitable, timeout):
        raise TimeoutError

    with (
        patch("multiprocessing.get_context", return_value=fake_ctx),
        patch("asyncio.wait_for", side_effect=_raise_timeout),
    ):
        adapter = ScrapyAdapter(timeout_seconds=0.01)
        adapter._available = True
        result = await adapter.run_spider("titles", ["http://example.com"])

    assert result == []
    fake_process.terminate.assert_called_once()


class TestAvailabilityDetection:
    def test_available_false_when_scrapy_not_importable(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "scrapy":
                raise ImportError("no scrapy")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        adapter = ScrapyAdapter()
        assert adapter._available is False


class TestRunSpiderSubprocess:
    """Exercises the real subprocess entry point in-process, with
    scrapy.crawler.CrawlerProcess faked so no real Twisted reactor starts —
    same "mock the subprocess boundary" approach the module docstring
    describes, one level deeper (the function itself, not multiprocessing)."""

    def test_collects_items_from_parsed_responses(self, monkeypatch):
        captured = {}

        class FakeCrawlerProcess:
            def __init__(self, settings):
                captured["settings"] = settings

            def crawl(self, spider_cls):
                captured["spider_cls"] = spider_cls

            def start(self):
                spider = captured["spider_cls"]()
                fake_response = MagicMock()
                fake_response.url = "http://example.com"
                fake_response.css.return_value.get.return_value = "Example Title"
                list(spider.parse(fake_response))

        monkeypatch.setattr("scrapy.crawler.CrawlerProcess", FakeCrawlerProcess)
        monkeypatch.setattr("scrapy.utils.project.get_project_settings", lambda: {})

        queue = MagicMock()
        sa._run_spider_subprocess("titles", ["http://example.com"], queue)

        queue.put.assert_called_once_with([{"url": "http://example.com", "title": "Example Title"}])

    def test_puts_exception_on_queue_when_crawl_fails(self, monkeypatch):
        monkeypatch.setattr(
            "scrapy.utils.project.get_project_settings",
            MagicMock(side_effect=RuntimeError("settings boom")),
        )

        queue = MagicMock()
        sa._run_spider_subprocess("titles", ["http://example.com"], queue)

        queue.put.assert_called_once()
        put_arg = queue.put.call_args[0][0]
        assert isinstance(put_arg, RuntimeError)
        assert str(put_arg) == "settings boom"
