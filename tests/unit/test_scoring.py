# tests/unit/test_scoring.py
"""Proxy scoring engine tests — spec §3.3."""

from scraper_engine.core.models import AnonymityLevel, AsnClass
from scraper_engine.proxy.scoring import ScoringEngine, compute_success_rate


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

    def test_apply_success_trims_history_to_last_50_samples(self) -> None:
        engine = ScoringEngine()
        for i in range(60):
            engine.apply_success("9.9.9.9", 80, i)
        history = engine._latency_history["9.9.9.9:80"]
        assert len(history) == 50
        assert history[0] == 10.0  # oldest 10 samples (0..9) trimmed away
        assert history[-1] == 59.0

    def test_average_latency_returns_none_when_no_data(self) -> None:
        engine = ScoringEngine()
        assert engine.average_latency("1.1.1.1", 9999) is None

    def test_first_time_elite_residential_fast_proxy_clears_l2_and_l3(self) -> None:
        """Regression test (round 32): before this fix, success_rate
        defaulting to 50.0 and weighted at 45% meant even a theoretically
        perfect first-time proxy topped out around 56/100 — below L2's 70
        and L3's 90, making it mathematically impossible for ANY proxy to
        ever clear those tiers regardless of real quality. success_rate=None
        (no real track record yet) must redistribute that weight instead."""
        engine = ScoringEngine()
        perfect = engine.compute_score(
            latency_ms=0,
            success_rate=None,
            anonymity=AnonymityLevel.ELITE,
            asn=AsnClass.RESIDENTIAL,
            last_validated_seconds_ago=0,
        )
        assert perfect.total >= 90.0

    def test_first_time_transparent_unknown_proxy_stays_below_l2(self) -> None:
        """The realistic free-proxy case (transparent anonymity, unknown ASN
        — this session's live pool was 100% this combination) must still
        score meaningfully below L2's 70 threshold on first validation alone
        — the fix makes L2 reachable for genuinely good proxies, not
        universally easier to reach."""
        engine = ScoringEngine()
        realistic = engine.compute_score(
            latency_ms=200,
            success_rate=None,
            anonymity=AnonymityLevel.TRANSPARENT,
            asn=AsnClass.UNKNOWN,
            last_validated_seconds_ago=0,
        )
        assert realistic.total < 70.0

    def test_real_success_rate_can_push_proven_proxy_past_l2(self) -> None:
        """Once real usage history exists (success_rate is a measured
        value, not None), a proxy that keeps succeeding should be able to
        climb into L2 territory even without elite anonymity — the
        "proven over time" half of the lifecycle."""
        engine = ScoringEngine()
        proven = engine.compute_score(
            latency_ms=200,
            success_rate=95.0,
            anonymity=AnonymityLevel.TRANSPARENT,
            asn=AsnClass.UNKNOWN,
            last_validated_seconds_ago=0,
        )
        assert proven.total >= 70.0


class TestComputeSuccessRate:
    def test_none_at_zero_attempts(self) -> None:
        assert compute_success_rate(0, 0) is None

    def test_correct_percentage(self) -> None:
        assert compute_success_rate(3, 1) == 75.0
        assert compute_success_rate(0, 5) == 0.0
        assert compute_success_rate(5, 0) == 100.0
