"""Dependency-free probability calibration with causal fit cutoffs."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Self

from kalshi_bot.domain import utc_datetime


@dataclass(frozen=True)
class PredictionSample:
    """A forecast and its subsequently observed binary outcome."""

    prediction: float
    outcome: float
    prediction_timestamp: datetime
    outcome_timestamp: datetime

    def __post_init__(self) -> None:
        if not math.isfinite(self.prediction) or not 0.0 <= self.prediction <= 1.0:
            raise ValueError("prediction must be finite and within [0, 1]")
        if self.outcome not in (0, 0.0, 1, 1.0, False, True):
            raise ValueError("outcome must be binary")
        prediction_time = utc_datetime(self.prediction_timestamp)
        outcome_time = utc_datetime(self.outcome_timestamp)
        if outcome_time < prediction_time:
            raise ValueError("outcome_timestamp cannot precede prediction_timestamp")
        object.__setattr__(self, "prediction_timestamp", prediction_time)
        object.__setattr__(self, "outcome_timestamp", outcome_time)
        object.__setattr__(self, "outcome", float(self.outcome))


@dataclass(frozen=True)
class ReliabilityBin:
    """Observed reliability statistics for one probability interval."""

    lower: float
    upper: float
    count: int
    mean_prediction: float
    empirical_frequency: float
    calibrated_probability: float


@dataclass(frozen=True)
class CalibrationMetrics:
    """Aggregate probabilistic forecast quality metrics."""

    sample_count: int
    brier_score: float
    calibration_error: float


class ProbabilityCalibrator:
    """Piecewise-constant beta-binomial probability calibrator.

    A fit always requires a validation decision cutoff. Both prediction and
    outcome must be strictly earlier than that cutoff, preventing a label that
    became known at or after validation time from entering training.
    """

    def __init__(
        self,
        n_bins: int = 10,
        *,
        alpha: float = 1.0,
        beta: float = 1.0,
    ) -> None:
        if n_bins <= 0:
            raise ValueError("n_bins must be positive")
        if not math.isfinite(alpha) or not math.isfinite(beta) or alpha <= 0 or beta <= 0:
            raise ValueError("alpha and beta must be positive and finite")
        self.n_bins = n_bins
        self.alpha = alpha
        self.beta = beta
        self._samples: list[PredictionSample] = []
        self._successes: list[float] = [0.0] * n_bins
        self._counts: list[int] = [0] * n_bins
        self._fit_cutoff: datetime | None = None

    @property
    def samples(self) -> tuple[PredictionSample, ...]:
        """Return all stored samples without exposing mutable storage."""
        return tuple(self._samples)

    @property
    def fit_cutoff(self) -> datetime | None:
        """Return the validation timestamp used for the current fit."""
        return self._fit_cutoff

    @property
    def fitted_sample_count(self) -> int:
        """Return the number of causally eligible samples in the current fit."""
        return sum(self._counts)

    def add_sample(
        self,
        prediction: float,
        outcome: float | bool,
        prediction_timestamp: datetime,
        outcome_timestamp: datetime | None = None,
    ) -> PredictionSample:
        """Store one timestamped prediction/outcome pair.

        Adding data does not silently alter an existing fitted transform.
        Call :meth:`fit` again with an explicit cutoff to use new samples.
        """
        sample = PredictionSample(
            prediction=float(prediction),
            outcome=float(outcome),
            prediction_timestamp=prediction_timestamp,
            outcome_timestamp=outcome_timestamp or prediction_timestamp,
        )
        self._samples.append(sample)
        return sample

    add = add_sample

    def extend(self, samples: list[PredictionSample] | tuple[PredictionSample, ...]) -> None:
        """Store validated samples without fitting them."""
        self._samples.extend(samples)

    def _bin_index(self, probability: float) -> int:
        return min(int(probability * self.n_bins), self.n_bins - 1)

    def _eligible(self, cutoff: datetime) -> tuple[PredictionSample, ...]:
        return tuple(
            sample
            for sample in self._samples
            if sample.prediction_timestamp < cutoff and sample.outcome_timestamp < cutoff
        )

    def fit(self, validation_decision_timestamp: datetime) -> Self:
        """Fit only samples fully observed before validation decision time."""
        cutoff = utc_datetime(validation_decision_timestamp)
        counts = [0] * self.n_bins
        successes = [0.0] * self.n_bins
        for sample in self._eligible(cutoff):
            index = self._bin_index(sample.prediction)
            counts[index] += 1
            successes[index] += sample.outcome
        self._counts = counts
        self._successes = successes
        self._fit_cutoff = cutoff
        return self

    def transform(self, probability: float) -> float:
        """Return the beta-shrunk observed frequency for a probability."""
        value = float(probability)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("probability must be finite and within [0, 1]")
        if self._fit_cutoff is None:
            raise RuntimeError("calibrator must be fitted before transform")
        index = self._bin_index(value)
        return (self._successes[index] + self.alpha) / (
            self._counts[index] + self.alpha + self.beta
        )

    def __call__(self, probability: float) -> float:
        """Calibrate a probability, making the instance model-callable."""
        return self.transform(probability)

    def reliability_bins(
        self,
        *,
        cutoff: datetime | None = None,
    ) -> tuple[ReliabilityBin, ...]:
        """Return fixed-width reliability bins with beta shrinkage."""
        if cutoff is None:
            if self._fit_cutoff is None:
                samples = tuple(self._samples)
            else:
                samples = self._eligible(self._fit_cutoff)
        else:
            samples = self._eligible(utc_datetime(cutoff))
        grouped: list[list[PredictionSample]] = [[] for _ in range(self.n_bins)]
        for sample in samples:
            grouped[self._bin_index(sample.prediction)].append(sample)
        bins: list[ReliabilityBin] = []
        for index, entries in enumerate(grouped):
            count = len(entries)
            successes = sum(entry.outcome for entry in entries)
            midpoint = (index + 0.5) / self.n_bins
            bins.append(
                ReliabilityBin(
                    lower=index / self.n_bins,
                    upper=(index + 1) / self.n_bins,
                    count=count,
                    mean_prediction=(
                        sum(entry.prediction for entry in entries) / count
                        if count
                        else midpoint
                    ),
                    empirical_frequency=successes / count if count else 0.0,
                    calibrated_probability=(successes + self.alpha)
                    / (count + self.alpha + self.beta),
                )
            )
        return tuple(bins)

    def metrics(self, *, cutoff: datetime | None = None) -> CalibrationMetrics:
        """Compute Brier score and expected calibration error."""
        if cutoff is None:
            samples = tuple(self._samples)
        else:
            samples = self._eligible(utc_datetime(cutoff))
        if not samples:
            return CalibrationMetrics(sample_count=0, brier_score=0.0, calibration_error=0.0)
        brier = sum((sample.prediction - sample.outcome) ** 2 for sample in samples) / len(
            samples
        )
        bins = self._bins_for_samples(samples)
        calibration_error = sum(
            item.count
            * abs(item.mean_prediction - item.empirical_frequency)
            / len(samples)
            for item in bins
            if item.count
        )
        return CalibrationMetrics(
            sample_count=len(samples),
            brier_score=brier,
            calibration_error=calibration_error,
        )

    def brier_score(self, *, cutoff: datetime | None = None) -> float:
        """Return mean squared probability error."""
        return self.metrics(cutoff=cutoff).brier_score

    def calibration_error(self, *, cutoff: datetime | None = None) -> float:
        """Return count-weighted expected calibration error."""
        return self.metrics(cutoff=cutoff).calibration_error

    def _bins_for_samples(
        self,
        samples: tuple[PredictionSample, ...],
    ) -> tuple[ReliabilityBin, ...]:
        grouped: list[list[PredictionSample]] = [[] for _ in range(self.n_bins)]
        for sample in samples:
            grouped[self._bin_index(sample.prediction)].append(sample)
        result: list[ReliabilityBin] = []
        for index, entries in enumerate(grouped):
            count = len(entries)
            successes = sum(entry.outcome for entry in entries)
            result.append(
                ReliabilityBin(
                    lower=index / self.n_bins,
                    upper=(index + 1) / self.n_bins,
                    count=count,
                    mean_prediction=(
                        sum(entry.prediction for entry in entries) / count if count else 0.0
                    ),
                    empirical_frequency=successes / count if count else 0.0,
                    calibrated_probability=(successes + self.alpha)
                    / (count + self.alpha + self.beta),
                )
            )
        return tuple(result)

    def to_dict(self) -> dict[str, object]:
        """Return JSON-serializable calibrator state."""
        return {
            "version": 1,
            "n_bins": self.n_bins,
            "alpha": self.alpha,
            "beta": self.beta,
            "fit_cutoff": self._fit_cutoff.isoformat() if self._fit_cutoff else None,
            "counts": list(self._counts),
            "successes": list(self._successes),
            "samples": [
                {
                    **asdict(sample),
                    "prediction_timestamp": sample.prediction_timestamp.isoformat(),
                    "outcome_timestamp": sample.outcome_timestamp.isoformat(),
                }
                for sample in self._samples
            ],
        }

    def save(self, path: str | Path) -> None:
        """Persist all samples and fitted state as JSON."""
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> Self:
        """Restore calibrator state from a decoded JSON object."""
        calibrator = cls(
            n_bins=int(payload["n_bins"]),
            alpha=float(payload["alpha"]),
            beta=float(payload["beta"]),
        )
        raw_samples = payload.get("samples", [])
        if not isinstance(raw_samples, list):
            raise ValueError("samples must be an array")
        for raw in raw_samples:
            if not isinstance(raw, dict):
                raise ValueError("sample must be an object")
            calibrator.add_sample(
                prediction=float(raw["prediction"]),
                outcome=float(raw["outcome"]),
                prediction_timestamp=datetime.fromisoformat(str(raw["prediction_timestamp"])),
                outcome_timestamp=datetime.fromisoformat(str(raw["outcome_timestamp"])),
            )
        raw_cutoff = payload.get("fit_cutoff")
        if raw_cutoff is not None:
            calibrator.fit(datetime.fromisoformat(str(raw_cutoff)))
            saved_counts = payload.get("counts")
            saved_successes = payload.get("successes")
            if saved_counts != calibrator._counts or saved_successes != calibrator._successes:
                raise ValueError("saved fitted state is inconsistent with samples and cutoff")
        return calibrator

    @classmethod
    def load(cls, path: str | Path) -> Self:
        """Load a calibrator from JSON."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("calibrator JSON must contain an object")
        return cls.from_dict(payload)


CalibrationModel = ProbabilityCalibrator
