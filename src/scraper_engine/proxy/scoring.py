# proxy/scoring.py
"""Multi-dimensional proxy scoring engine.

Scores proxies across dimensions:
  - Latency to target domain
  - Success rate (global + per-domain)
  - Anonymity level
  - ASN diversity
  - Recency of last validation
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scraper_engine.core.models import AnonymityLevel, AsnClass


@dataclass
class ProxyScore:
    """Composite proxy score with dimension breakdown."""

    total: float = 50.0
    latency_score: float = 50.0
    success_rate: float = 50.0
    anonymity_bonus: float = 0.0
    recency_penalty: float = 0.0
    breakdown: dict[str, float] = field(default_factory=dict)


def compute_success_rate(successes: int, failures: int) -> float | None:
    """None when there's no real track record yet (zero attempts) — lets
    compute_score() distinguish "genuinely unknown" from "measured 50%",
    which a bare float default couldn't (round 32)."""
    total = successes + failures
    if total == 0:
        return None
    return (successes / total) * 100.0


class ScoringEngine:
    """Compute and update multi-dimensional proxy scores.

    Bonuses are 0-100 scaled (round 32 — previously small flat point
    values like 15/10 multiplied again by their own 0.15/0.10 weight,
    capping their real max contribution at ~2.25/~1.0 out of 100,
    effectively disabling them; found while wiring this formula into
    production for the first time — it was dead code before, so this
    scaling bug was never exercised against real thresholds).
    """

    # Anonymity level bonuses (0-100 scale, matches latency_score's convention)
    ANONYMITY_BONUS: dict[AnonymityLevel, float] = {
        AnonymityLevel.ELITE: 100.0,
        AnonymityLevel.ANONYMOUS: 50.0,
        AnonymityLevel.TRANSPARENT: 0.0,
    }

    # ASN class bonuses (residential/mobile proxies preferred for anti-detection)
    ASN_BONUS: dict[AsnClass, float] = {
        AsnClass.RESIDENTIAL: 100.0,
        AsnClass.MOBILE: 100.0,
        AsnClass.DATACENTER: 0.0,
        AsnClass.UNKNOWN: 0.0,
    }

    # Base weights when a real success_rate is known.
    _W_LATENCY = 0.25
    _W_SUCCESS = 0.45
    _W_ANONYMITY = 0.15
    _W_ASN = 0.10
    _W_RECENCY = 0.05
    # Sum of the non-success-rate weights — used to proportionally
    # redistribute success_rate's 0.45 share when there's no real data yet
    # (see compute_score's success_rate=None branch).
    _W_NO_SUCCESS_TOTAL = _W_LATENCY + _W_ANONYMITY + _W_ASN + _W_RECENCY

    def __init__(self) -> None:
        self._latency_history: dict[str, list[float]] = {}

    def compute_score(
        self,
        latency_ms: int | None = None,
        success_rate: float | None = None,
        anonymity: AnonymityLevel = AnonymityLevel.TRANSPARENT,
        asn: AsnClass = AsnClass.UNKNOWN,
        last_validated_seconds_ago: float | None = None,
    ) -> ProxyScore:
        """Compute a composite score from multiple dimensions.

        success_rate=None (no real track record yet — e.g. a proxy's first
        ever validation) redistributes its 45% weight across the other
        four dimensions instead of scoring against an unearned guess
        (round 32 — an earlier default of 50.0 here made it mathematically
        impossible for ANY proxy, regardless of real quality, to ever
        reach L2/L3's score tiers; see decisions.md).
        """
        score = ProxyScore()

        # Latency: inverse scoring — lower latency = higher score
        if latency_ms is not None:
            score.latency_score = max(0.0, min(100.0, 100.0 - (latency_ms / 100.0)))

        # Anonymity bonus
        score.anonymity_bonus = self.ANONYMITY_BONUS.get(anonymity, 0.0)

        # ASN bonus
        asn_bonus = self.ASN_BONUS.get(asn, 0.0)

        # Recency penalty: decay score for proxies not validated recently
        recency_penalty = 0.0
        if last_validated_seconds_ago is not None:
            hours_ago = last_validated_seconds_ago / 3600
            recency_penalty = min(30.0, hours_ago * 2.0)
        score.recency_penalty = recency_penalty
        recency_score = 100.0 - recency_penalty

        if success_rate is None:
            score.success_rate = 50.0  # informational only; not used in total below
            total = (
                score.latency_score * (self._W_LATENCY / self._W_NO_SUCCESS_TOTAL)
                + score.anonymity_bonus * (self._W_ANONYMITY / self._W_NO_SUCCESS_TOTAL)
                + asn_bonus * (self._W_ASN / self._W_NO_SUCCESS_TOTAL)
                + recency_score * (self._W_RECENCY / self._W_NO_SUCCESS_TOTAL)
            )
        else:
            score.success_rate = max(0.0, min(100.0, success_rate))
            total = (
                score.latency_score * self._W_LATENCY
                + score.success_rate * self._W_SUCCESS
                + score.anonymity_bonus * self._W_ANONYMITY
                + asn_bonus * self._W_ASN
                + recency_score * self._W_RECENCY
            )

        score.total = max(0.0, min(100.0, total))
        score.breakdown = {
            "latency": score.latency_score,
            "success_rate": score.success_rate,
            "anonymity_bonus": score.anonymity_bonus,
            "asn_bonus": asn_bonus,
            "recency_penalty": recency_penalty,
        }
        return score

    def apply_success(self, ip: str, port: int, latency_ms: int) -> None:
        """Record successful fetch latency for moving average."""
        key = f"{ip}:{port}"
        if key not in self._latency_history:
            self._latency_history[key] = []
        self._latency_history[key].append(float(latency_ms))
        # Keep last 50 samples
        if len(self._latency_history[key]) > 50:
            self._latency_history[key] = self._latency_history[key][-50:]

    def apply_failure(self, ip: str, port: int, domain: str) -> None:
        """Penalize proxy on failure. Success rate decays externally via DB.

        Scoring impact handled by ProxyManager.mark_failure which decrements
        reliability_score in the database.
        """

    def average_latency(self, ip: str, port: int) -> float | None:
        """Return average latency for a proxy, or None if no data."""
        key = f"{ip}:{port}"
        samples = self._latency_history.get(key, [])
        if not samples:
            return None
        return sum(samples) / len(samples)
