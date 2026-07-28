# tests/unit/test_factory_capsolver_gate.py
"""LevelConfig.capsolver_enabled was defined and set in base.yaml but never
read anywhere — the real gate was just "is a solver configured." Now
fetcher/factory.py drops the solver per-level when disabled."""

from unittest.mock import MagicMock

from scraper_engine.config.schema import AppConfig, LevelConfig, LevelsConfig
from scraper_engine.fetcher.factory import build_level2_fetcher, build_level3_fetcher


def _config_with(capsolver_enabled: bool) -> AppConfig:
    return AppConfig(
        levels=LevelsConfig(
            level_2=LevelConfig(
                engine="botasaurus+camoufox", proxy_tier_min_score=70.0,
                timeout_seconds=40, capsolver_enabled=capsolver_enabled,
            ),
            level_3=LevelConfig(
                engine="camoufox", proxy_tier_min_score=90.0,
                timeout_seconds=60, capsolver_enabled=capsolver_enabled,
            ),
        )
    )


def test_level2_fetcher_drops_solver_when_disabled():
    solver = MagicMock()
    fetcher = build_level2_fetcher(_config_with(False), captcha_solver=solver)
    assert fetcher._captcha_solver is None


def test_level2_fetcher_keeps_solver_when_enabled():
    solver = MagicMock()
    fetcher = build_level2_fetcher(_config_with(True), captcha_solver=solver)
    assert fetcher._captcha_solver is solver


def test_level3_fetcher_drops_solver_when_disabled():
    solver = MagicMock()
    fetcher = build_level3_fetcher(_config_with(False), captcha_solver=solver)
    assert fetcher._captcha_solver is None


def test_level3_fetcher_keeps_solver_when_enabled():
    solver = MagicMock()
    fetcher = build_level3_fetcher(_config_with(True), captcha_solver=solver)
    assert fetcher._captcha_solver is solver
