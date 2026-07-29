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


class ScoringEngine:
    """Compute and update multi-dimensional proxy scores."""

    # Anonymity level bonuses
    ANONYMITY_BONUS: dict[AnonymityLevel, float] = {
        AnonymityLevel.ELITE: 15.0,
        AnonymityLevel.ANONYMOUS: 7.0,
        AnonymityLevel.TRANSPARENT: 0.0,
    }

    # ASN class bonuses (residential/mobile proxies preferred for anti-detection)
    ASN_BONUS: dict[AsnClass, float] = {
        AsnClass.RESIDENTIAL: 10.0,
        AsnClass.MOBILE: 10.0,
        AsnClass.DATACENTER: 0.0,
        AsnClass.UNKNOWN: 0.0,
    }

    def __init__(self) -> None:
        self._latency_history: dict[str, list[float]] = {}

    def compute_score(
        self,
        latency_ms: int | None = None,
        success_rate: float = 50.0,
        anonymity: AnonymityLevel = AnonymityLevel.TRANSPARENT,
        asn: AsnClass = AsnClass.UNKNOWN,
        last_validated_seconds_ago: float | None = None,
    ) -> ProxyScore:
        """Compute a composite score from multiple dimensions."""
        score = ProxyScore()

        # Latency: inverse scoring — lower latency = higher score
        if latency_ms is not None:
            score.latency_score = max(0.0, min(100.0, 100.0 - (latency_ms / 100.0)))

        # Success rate passed directly
        score.success_rate = max(0.0, min(100.0, success_rate))

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

        # Composite: weighted average with bonuses
        score.total = max(
            0.0,
            min(
                100.0,
                score.latency_score * 0.25
                + score.success_rate * 0.45
                + score.anonymity_bonus * 0.15
                + asn_bonus * 0.10
                + (100.0 - recency_penalty) * 0.05,
            ),
        )

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
