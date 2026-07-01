"""Baseline & anomaly engine.

Per-metric rolling statistics with a bounded window (so an outlier can't poison the
mean forever) plus an EWMA for recent-bias tracking. The anomaly decision is a plain
z-score test gated behind a cold-start minimum so a fresh metric never screams on its
first few samples.

This module is pure math: no I/O, no sensors, no sqlite. That isolation is what makes
the behaviour trustworthy — it can be tested exhaustively without spinning up a box.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

_DEFAULT_METRIC = "_default"

# Floor on the standard deviation used in the z-score, expressed as a fraction of the
# metric's scale (|mean|, or 1.0 for near-zero means). This prevents the z-score from
# exploding on a rock-steady stream (where std ~0 would make any tiny jitter look like
# a 100σ anomaly) while still flagging genuinely large jumps. 5% of scale means a value
# must move ~15% off the mean at z_threshold=3 to count as anomalous when the stream
# has been constant — generous enough to ignore noise, strict enough to catch spikes.
_MIN_STD_FRACTION = 0.05


@dataclass
class UpdateResult:
    """The outcome of observing one value for one metric."""

    value: float
    is_anomaly: bool
    z_score: float
    mean: float
    std: float
    n: int
    ewma: float
    cold_start: bool = False


class _MetricStats:
    """Rolling mean/std + EWMA for a single metric stream."""

    __slots__ = ("window_size", "values", "sum", "sumsq", "ewma", "ewma_alpha", "_has_ewma")

    def __init__(self, window_size: int, ewma_alpha: float):
        self.window_size = window_size
        self.values: deque[float] = deque(maxlen=window_size)
        self.sum = 0.0
        self.sumsq = 0.0
        self.ewma = 0.0
        self.ewma_alpha = ewma_alpha
        self._has_ewma = False

    def add(self, value: float) -> tuple[float, float, int, float]:
        """Record a value. Returns (mean, std, n, ewma)."""
        # If the window is full, the deque will silently evict the oldest on append;
        # subtract its contribution from the running sums first so they stay honest.
        if len(self.values) == self.values.maxlen:
            evicted = self.values[0]
            self.sum -= evicted
            self.sumsq -= evicted * evicted
        self.values.append(value)
        self.sum += value
        self.sumsq += value * value

        n = len(self.values)
        mean = self.sum / n if n else 0.0
        if n > 1:
            # Population variance from running sums. The E[X^2]-E[X]^2 form can drift
            # numerically; clamp the small negative that float error can produce.
            var = self.sumsq / n - mean * mean
            if var < 0.0:
                var = 0.0
            std = math.sqrt(var)
            # Floor std so a near-constant stream (tiny float drift) doesn't make the
            # z-score explode for a marginally different value. 1e-9 is well below any
            # real signal we care about.
            if std < 1e-9:
                std = 0.0
        else:
            std = 0.0

        if not self._has_ewma:
            self.ewma = value
            self._has_ewma = True
        else:
            a = self.ewma_alpha
            self.ewma = a * value + (1.0 - a) * self.ewma

        return mean, std, n, self.ewma

    @property
    def n(self) -> int:
        return len(self.values)


def _std_of_stats(stats: _MetricStats) -> float:
    """Population std from a stats object's running sums. Pure, non-mutating."""
    if stats.n < 2:
        return 0.0
    mean = stats.sum / stats.n
    var = stats.sumsq / stats.n - mean * mean
    if var < 0.0:
        var = 0.0
    return math.sqrt(var)


class Baseline:
    """Tracks rolling stats for many metrics and answers 'is this value normal?'."""

    def __init__(
        self,
        window_size: int = 2880,
        ewma_alpha: float = 0.1,
        z_threshold: float = 3.0,
        min_samples: int = 30,
    ):
        self.window_size = window_size
        self.ewma_alpha = ewma_alpha
        self.z_threshold = z_threshold
        self.min_samples = min_samples
        self._metrics: dict[str, _MetricStats] = {}

    def _stats(self, metric: str) -> _MetricStats:
        s = self._metrics.get(metric)
        if s is None:
            s = _MetricStats(self.window_size, self.ewma_alpha)
            self._metrics[metric] = s
        return s

    def update(self, metric: str | float, value: float | None = None) -> UpdateResult:
        """Observe a value.

        Can be called two ways:
            b.update(42.0)            # default metric stream
            b.update("cpu", 42.0)     # named metric
        """
        if value is None:
            # Single-argument form: the first arg is the value, metric is default.
            assert isinstance(metric, (int, float)), "value must be a number"
            metric_name = _DEFAULT_METRIC
            val = float(metric)
        else:
            metric_name = str(metric)
            val = float(value)

        stats = self._stats(metric_name)
        mean, std, n, ewma = stats.add(val)
        return self._decide(metric_name, val, mean, std, n, ewma)

    def evaluate(self, metric: str, value: float) -> UpdateResult:
        """Judge `value` against the current baseline for `metric` WITHOUT recording it.

        Used by the MCP server's is_this_normal: the queried value must not mutate the
        baseline (no pollution across calls) and must not inflate n. Returns the same
        UpdateResult shape as update(), with cold_start set when n < min_samples.
        """
        metric_name = str(metric)
        val = float(value)
        stats = self._stats(metric_name)
        if stats.n == 0:
            return UpdateResult(
                value=val, is_anomaly=False, z_score=0.0,
                mean=0.0, std=0.0, n=0, ewma=0.0, cold_start=True,
            )
        mean = stats.sum / stats.n
        std = _std_of_stats(stats)
        ewma = stats.ewma
        return self._decide(metric_name, val, mean, std, stats.n, ewma)

    def _decide(self, metric_name, val, mean, std, n, ewma) -> UpdateResult:
        """Shared anomaly decision for update() and evaluate(). Does not mutate."""
        # Effective std floors the denominator so a constant stream (std ~0) doesn't
        # make the z-score explode on jitter — while still flagging large jumps. The
        # floor is a small fraction of the metric's scale (|mean|, or 1.0 for means
        # near zero). With _MIN_STD_FRACTION=0.05 and z_threshold=3, a constant stream
        # needs a ~15% off-mean jump to flag; a stream with real spread uses the
        # measured std directly.
        scale = max(abs(mean), 1.0)
        effective_std = max(std, _MIN_STD_FRACTION * scale)

        if n < self.min_samples or effective_std == 0.0:
            z = 0.0 if n < self.min_samples else abs(val - mean) / effective_std
        else:
            z = abs(val - mean) / effective_std

        is_anomaly = n >= self.min_samples and z > self.z_threshold
        return UpdateResult(
            value=val,
            is_anomaly=is_anomaly,
            z_score=z,
            mean=mean,
            std=std,
            n=n,
            ewma=ewma,
            cold_start=n < self.min_samples,
        )

    def stats_for(self, metric: str) -> _MetricStats | None:
        return self._metrics.get(metric)

    @property
    def metrics(self) -> list[str]:
        return list(self._metrics.keys())

    # --- convenience accessors for the default metric stream ---
    # Tests and simple scripts often use a single metric; these read through to the
    # default metric's stats without requiring callers to thread the metric name.

    @property
    def _default(self) -> _MetricStats:
        return self._stats(_DEFAULT_METRIC)

    @property
    def n(self) -> int:
        return self._default.n

    @property
    def ewma(self) -> float:
        return self._default.ewma

    @property
    def mean(self) -> float:
        s = self._default
        return s.sum / s.n if s.n else 0.0

    @property
    def std(self) -> float:
        s = self._default
        if s.n < 2:
            return 0.0
        var = s.sumsq / s.n - (s.sum / s.n) ** 2
        return math.sqrt(max(var, 0.0))
