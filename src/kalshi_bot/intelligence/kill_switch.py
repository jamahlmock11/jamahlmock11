"""Confidence kill switch: halt trading when recent accuracy collapses."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class KillSwitchState:
  halted: bool
  reason: str
  recent_accuracy: float
  sample_count: int
  recovery_threshold: float


class ConfidenceKillSwitch:
    """Stop trading when the last N predictions are below accuracy threshold."""

    def __init__(
        self,
        *,
        window_size: int = 25,
        halt_accuracy: float = 0.55,
        recovery_accuracy: float = 0.60,
        min_samples: int = 25,
    ) -> None:
        if window_size <= 0:
            raise ValueError("window_size must be positive")
        self.window_size = window_size
        self.halt_accuracy = halt_accuracy
        self.recovery_accuracy = recovery_accuracy
        self.min_samples = min_samples
        self._outcomes: deque[bool] = deque(maxlen=window_size)
        self._halted = False
        self._halt_reason = ""

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    def record_outcome(self, correct: bool) -> None:
        """Record whether a resolved prediction was directionally correct."""
        self._outcomes.append(correct)
        self._evaluate()

    def record_prediction_result(self, predicted_up: bool, actual_up: bool) -> None:
        """Record outcome from predicted and actual direction."""
        self.record_outcome(predicted_up == actual_up)

    def _accuracy(self) -> float:
        if not self._outcomes:
            return 1.0
        return sum(self._outcomes) / len(self._outcomes)

    def _evaluate(self) -> None:
        if len(self._outcomes) < self.min_samples:
            return
        accuracy = self._accuracy()
        if self._halted:
            if accuracy >= self.recovery_accuracy:
                self._halted = False
                self._halt_reason = ""
        elif accuracy < self.halt_accuracy:
            self._halted = True
            self._halt_reason = (
                f"kill switch: last {len(self._outcomes)} predictions "
                f"accuracy {accuracy:.1%} < {self.halt_accuracy:.0%}"
            )

    def check(self) -> KillSwitchState:
        """Return current kill switch state without recording."""
        accuracy = self._accuracy()
        return KillSwitchState(
            halted=self._halted,
            reason=self._halt_reason,
            recent_accuracy=accuracy,
            sample_count=len(self._outcomes),
            recovery_threshold=self.recovery_accuracy,
        )

    def should_trade(self) -> tuple[bool, str]:
        """Whether trading is allowed under kill switch rules."""
        state = self.check()
        if state.halted:
            return False, state.reason
        return True, ""

    def hydrate(self, outcomes: list[bool]) -> None:
        """Load historical outcomes (e.g. from journal)."""
        self._outcomes.clear()
        for outcome in outcomes[-self.window_size:]:
            self._outcomes.append(outcome)
        self._evaluate()
