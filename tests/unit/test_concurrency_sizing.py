"""Round 64 — URL concurrency vs. the browser ceiling.

`politeness.max_concurrent_urls_per_job` and `camoufox.max_total_instances`
were tuned independently; nothing checked that a job could actually get a
browser for every URL it was allowed to run at once.
"""

import logging

import pytest
from pydantic import ValidationError

import scraper_engine.orchestrator.tasks as tasks_module
from scraper_engine.config.schema import AppConfig, CamoufoxConfig, PolitenessConfig


def test_defaults_are_consistent():
    config = AppConfig()
    assert config.politeness.max_concurrent_urls_per_job <= config.camoufox.max_total_instances


def test_more_concurrent_urls_than_browsers_is_rejected():
    with pytest.raises(ValidationError, match="max_concurrent_urls_per_job"):
        AppConfig(
            politeness=PolitenessConfig(max_concurrent_urls_per_job=9),
            camoufox=CamoufoxConfig(max_total_instances=8),
        )


def test_equal_is_allowed():
    AppConfig(
        politeness=PolitenessConfig(max_concurrent_urls_per_job=8),
        camoufox=CamoufoxConfig(max_total_instances=8),
    )


def test_warns_when_the_resolved_ceiling_drops_below_url_concurrency(caplog):
    with caplog.at_level(logging.WARNING, logger=tasks_module.__name__):
        tasks_module._warn_if_ceiling_below_url_concurrency(3, 5)
    assert "browser_ceiling_below_url_concurrency" in caplog.text


def test_silent_when_the_ceiling_is_enough(caplog):
    with caplog.at_level(logging.WARNING, logger=tasks_module.__name__):
        tasks_module._warn_if_ceiling_below_url_concurrency(8, 5)
    assert caplog.text == ""


def test_silent_under_host_admission_even_below_url_concurrency(caplog):
    """Round 65 — the host-wide limit binds then; the local ceiling is only
    a safety net, so queueing on it is expected."""
    with caplog.at_level(logging.WARNING, logger=tasks_module.__name__):
        tasks_module._warn_if_ceiling_below_url_concurrency(3, 5, True)
    assert caplog.text == ""
