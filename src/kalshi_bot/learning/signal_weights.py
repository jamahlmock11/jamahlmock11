"""Per-signal accuracy tracking and nightly weight updates."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SIGNALS = ("ema", "rsi", "vwap", "bollinger", "orderbook", "news")


@dataclass
class SignalRecord:
    correct: int = 0
    total: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total > 0 else 0.5


@dataclass
class SignalWeightTracker:
    """
    Track individual signal accuracy and update weights nightly.

    New Weight = Old Weight × Accuracy
    """

    weights: dict[str, float] = field(default_factory=lambda: dict.fromkeys(DEFAULT_SIGNALS, 1.0))
    records: dict[str, SignalRecord] = field(
        default_factory=lambda: {name: SignalRecord() for name in DEFAULT_SIGNALS}
    )
    minimum_samples: int = 10

    def record_signal_outcomes(
        self,
        signal_probs: dict[str, float],
        actual_up: bool,
    ) -> None:
        """Record whether each signal predicted the correct direction."""
        for name, prob in signal_probs.items():
            if name not in self.records:
                self.records[name] = SignalRecord()
            predicted_up = prob >= 0.5
            correct = predicted_up == actual_up
            self.records[name].total += 1
            if correct:
                self.records[name].correct += 1

    def should_update(self) -> bool:
        return any(record.total >= self.minimum_samples for record in self.records.values())

    def accuracies(self) -> dict[str, float]:
        return {name: record.accuracy for name, record in self.records.items()}

    def update_weights(self) -> dict[str, float]:
        """
        Nightly weight update: New Weight = Old Weight × Accuracy.

        Normalizes weights to preserve relative scale.
        """
        new_weights: dict[str, float] = {}
        for name, old_weight in self.weights.items():
            record = self.records.get(name, SignalRecord())
            accuracy = record.accuracy if record.total >= self.minimum_samples else 0.5
            new_weights[name] = old_weight * max(accuracy, 0.1)

        total = sum(new_weights.values())
        if total > 0:
            scale = len(new_weights) / total
            new_weights = {name: weight * scale for name, weight in new_weights.items()}

        self.weights = new_weights
        return dict(new_weights)

    def apply_weights(self, base_weights: dict[str, float]) -> dict[str, float]:
        """Multiply base regime weights by learned signal weights."""
        combined: dict[str, float] = {}
        for name, base in base_weights.items():
            learned = self.weights.get(name, 1.0)
            combined[name] = base * learned
        total = sum(combined.values())
        if total <= 0:
            return base_weights
        return {name: weight / total for name, weight in combined.items()}

    def to_dict(self) -> dict[str, object]:
        return {
            "weights": self.weights,
            "records": {
                name: {"correct": rec.correct, "total": rec.total}
                for name, rec in self.records.items()
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> SignalWeightTracker:
        tracker = SignalWeightTracker()
        raw_weights = payload.get("weights", {})
        if isinstance(raw_weights, dict):
            tracker.weights = {str(k): float(v) for k, v in raw_weights.items()}
        raw_records = payload.get("records", {})
        if isinstance(raw_records, dict):
            for name, data in raw_records.items():
                if isinstance(data, dict):
                    tracker.records[str(name)] = SignalRecord(
                        correct=int(data.get("correct", 0)),
                        total=int(data.get("total", 0)),
                    )
        return tracker

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> SignalWeightTracker:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("signal weights JSON must be an object")
        return cls.from_dict(payload)
