"""Historical pattern matching against prior trade setups."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from kalshi_bot.domain import FeatureSnapshot, Regime
from kalshi_bot.features.enriched import EnrichedFeatures


@dataclass(frozen=True)
class PatternMatchResult:
    """Outcome of similarity search against historical trades."""

    match_count: int
    win_rate: float
    average_pnl: float
    confidence: float  # 0–1 based on sample size
    similar_setup_found: bool
    recommendation: str


FEATURE_KEYS = (
    "z_distance",
    "short_trend",
    "orderbook_imbalance",
    "realized_vol",
    "seconds_remaining",
    "regime_code",
    "liquidity_score",
    "momentum_30s",
)


def _regime_code(regime: Regime) -> float:
    return float(list(Regime).index(regime)) / max(len(Regime) - 1, 1)


def _feature_vector(
    features: FeatureSnapshot,
    enriched: EnrichedFeatures,
    regime: Regime,
) -> list[float]:
    return [
        features.z_distance_to_strike,
        features.short_trend,
        features.orderbook_imbalance,
        features.realized_vol,
        features.seconds_remaining / 900.0,
        _regime_code(regime),
        enriched.microstructure.liquidity_score / 100.0,
        enriched.price_action.momentum_30s,
    ]


def _distance(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


class PatternMatcher:
    """Query prior trades for similar feature setups."""

    def __init__(
        self,
        journal_path: Path | str = Path("data/journal.db"),
        patterns_path: Path | str = Path("data/pattern_store.json"),
        *,
        similarity_threshold: float = 0.15,
        min_matches: int = 10,
    ) -> None:
        self.journal_path = Path(journal_path)
        self.patterns_path = Path(patterns_path)
        self.similarity_threshold = similarity_threshold
        self.min_matches = min_matches
        self.patterns_path.parent.mkdir(parents=True, exist_ok=True)

    def _load_stored_patterns(self) -> list[dict]:
        if not self.patterns_path.exists():
            return []
        try:
            return json.loads(self.patterns_path.read_text())
        except (json.JSONDecodeError, OSError):
            return []

    def _load_journal_patterns(self) -> list[dict]:
        if not self.journal_path.exists():
            return []
        patterns: list[dict] = []
        try:
            with sqlite3.connect(self.journal_path) as conn:
                rows = conn.execute(
                    """
                    SELECT payload, outcome, pnl, traded
                    FROM decisions
                    WHERE outcome IS NOT NULL AND traded = 1
                    ORDER BY ts DESC
                    LIMIT 2000
                    """,
                ).fetchall()
        except sqlite3.Error:
            return patterns

        for payload_raw, outcome, pnl, traded in rows:
            if not traded or outcome is None:
                continue
            try:
                payload = json.loads(payload_raw or "{}")
            except json.JSONDecodeError:
                continue
            features = payload.get("entry_features")
            if not features:
                continue
            patterns.append(
                {
                    "vector": [features.get(k, 0.0) for k in FEATURE_KEYS],
                    "outcome": float(outcome),
                    "pnl": float(pnl or 0.0),
                }
            )
        return patterns

    def match(
        self,
        features: FeatureSnapshot,
        enriched: EnrichedFeatures,
        regime: Regime,
    ) -> PatternMatchResult:
        query = _feature_vector(features, enriched, regime)
        candidates = self._load_stored_patterns() + self._load_journal_patterns()

        matches: list[dict] = []
        for candidate in candidates:
            vector = candidate.get("vector", [])
            if len(vector) != len(query):
                continue
            dist = _distance(query, vector)
            if dist <= self.similarity_threshold:
                matches.append({**candidate, "distance": dist})

        if not matches:
            return PatternMatchResult(
                match_count=0,
                win_rate=0.5,
                average_pnl=0.0,
                confidence=0.0,
                similar_setup_found=False,
                recommendation="insufficient historical evidence",
            )

        wins = sum(1 for m in matches if float(m.get("outcome", 0)) >= 0.5)
        win_rate = wins / len(matches)
        avg_pnl = sum(float(m.get("pnl", 0)) for m in matches) / len(matches)
        confidence = min(len(matches) / 50.0, 1.0)

        if len(matches) < self.min_matches:
            recommendation = f"only {len(matches)} similar setups (need {self.min_matches})"
        elif win_rate >= 0.55:
            recommendation = "historical evidence supports trade"
        elif win_rate <= 0.45:
            recommendation = "historical evidence opposes trade"
        else:
            recommendation = "historical evidence inconclusive"

        return PatternMatchResult(
            match_count=len(matches),
            win_rate=win_rate,
            average_pnl=avg_pnl,
            confidence=confidence,
            similar_setup_found=True,
            recommendation=recommendation,
        )

    def save_entry(
        self,
        features: FeatureSnapshot,
        enriched: EnrichedFeatures,
        regime: Regime,
        *,
        prediction: float,
        confidence: float,
        edge: float,
        action: str,
    ) -> None:
        """Persist entry features for future pattern matching."""
        entry = {
            "vector": dict(
                zip(
                    FEATURE_KEYS,
                    _feature_vector(features, enriched, regime),
                )
            ),
            "prediction": prediction,
            "confidence": confidence,
            "edge": edge,
            "action": action,
        }
        stored = self._load_stored_patterns()
        stored.append(entry)
        # Keep last 5000 entries
        stored = stored[-5000:]
        self.patterns_path.write_text(json.dumps(stored, indent=2))

    def record_outcome(
        self,
        entry_index: int,
        *,
        outcome: float,
        pnl: float,
    ) -> None:
        stored = self._load_stored_patterns()
        if 0 <= entry_index < len(stored):
            stored[entry_index]["outcome"] = outcome
            stored[entry_index]["pnl"] = pnl
            self.patterns_path.write_text(json.dumps(stored, indent=2))
