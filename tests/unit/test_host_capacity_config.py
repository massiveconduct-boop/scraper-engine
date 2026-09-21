# tests/unit/test_host_capacity_config.py
"""config/schema.py::HostCapacityConfig validation (round 65)."""

import pytest
from pydantic import ValidationError

from scraper_engine.config.schema import AppConfig, HostCapacityConfig


def test_defaults_are_off_and_coherent():
    cfg = AppConfig().host_capacity
    assert cfg.enabled is False
    assert cfg.max_units is None and cfg.default_units is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"min_units": 5, "max_units": 2}, "exceeds max_units"),
        ({"cpu_pressure_low": 80, "cpu_pressure_high": 80}, "cpu_pressure_low"),
        ({"poll_min_seconds": 2, "poll_max_seconds": 1}, "poll_min_seconds"),
        ({"lease_ttl_seconds": 30, "renew_interval_seconds": 15}, "renew_interval_seconds"),
    ],
)
def test_incoherent_settings_are_rejected(overrides, message):
    with pytest.raises(ValidationError, match=message):
        HostCapacityConfig(**overrides)


def test_max_units_may_be_set_explicitly():
    assert HostCapacityConfig(min_units=1, max_units=6).max_units == 6
