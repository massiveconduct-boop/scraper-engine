# tests/unit/test_scoring.py
"""Proxy scoring engine tests — spec §3.3."""

from scraper_engine.core.models import AnonymityLevel, AsnClass
from scraper_engine.proxy.scoring import ScoringEngine


class TestScoringEngine:
    def test_default_score(self) -> None:
        engine = ScoringEngine()
        score = engine.compute_score()
        assert 0 <= score.total <= 100
        assert score.latency_score == 50.0

    def test_fast_proxy_scores_high(self) -> None:
        engine = ScoringEngine()
        fast = engine.compute_score(latency_ms=10)
        slow = engine.compute_score(latency_ms=5000)
        assert fast.latency_score > slow.latency_score

    def test_elite_bonus(self) -> None:
        engine = ScoringEngine()
        elite = engine.compute_score(anonymity=AnonymityLevel.ELITE)
        transparent = engine.compute_score(anonymity=AnonymityLevel.TRANSPARENT)
        assert elite.anonymity_bonus > transparent.anonymity_bonus

    def test_residential_bonus(self) -> None:
        engine = ScoringEngine()
        residential = engine.compute_score(asn=AsnClass.RESIDENTIAL)
        datacenter = engine.compute_score(asn=AsnClass.DATACENTER)
        assert residential.total > datacenter.total

    def test_recency_penalty(self) -> None:
        engine = ScoringEngine()
        fresh = engine.compute_score(last_validated_seconds_ago=60)
        stale = engine.compute_score(last_validated_seconds_ago=86400 * 7)
        assert fresh.recency_penalty < stale.recency_penalty

    def test_high_success_rate_scores_high(self) -> None:
        engine = ScoringEngine()
        good = engine.compute_score(success_rate=95.0)
        bad = engine.compute_score(success_rate=10.0)
        assert good.total > bad.total

    def test_score_bounds(self) -> None:
        engine = ScoringEngine()
        # Best possible proxy
        best = engine.compute_score(
            latency_ms=1,
            success_rate=100.0,
            anonymity=AnonymityLevel.ELITE,
            asn=AsnClass.RESIDENTIAL,
            last_validated_seconds_ago=1,
        )
        assert 0 <= best.total <= 100

        # Worst possible proxy
        worst = engine.compute_score(
            latency_ms=30000,
            success_rate=0.0,
            anonymity=AnonymityLevel.TRANSPARENT,
            asn=AsnClass.DATACENTER,
            last_validated_seconds_ago=86400 * 365,
        )
        assert 0 <= worst.total <= 100
        assert best.total > worst.total

    def test_apply_success_tracks_latency(self) -> None:
        engine = ScoringEngine()
        engine.apply_success("1.2.3.4", 8080, 100)
        engine.apply_success("1.2.3.4", 8080, 200)
        avg = engine.average_latency("1.2.3.4", 8080)
        assert avg == 150.0

    def test_scoring_is_deterministic(self) -> None:
        engine = ScoringEngine()
        s1 = engine.compute_score(latency_ms=50, success_rate=80.0)
        s2 = engine.compute_score(latency_ms=50, success_rate=80.0)
        assert s1.total == s2.total
