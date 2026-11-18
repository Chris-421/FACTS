"""Planner efficiency and empirical delivery statistics for RFQ decisions."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from math import ceil, lgamma, log
from statistics import NormalDist

import numpy as np
import pandas as pd

from .experiments import _levels
from .model import minute_count, number


NS_PER_MINUTE = 60_000_000_000
DELIVERY_QUANTILES = {"p50": .50, "p80": .80, "p90": .90, "p95": .95}


@dataclass(frozen=True)
class PlannerEfficiency:
    estimate: float
    certainty: str
    minimum: float
    maximum: float
    half_weight_pp: dict = field(default_factory=lambda: {"low": 8., "medium": 4., "high": 2.})

    def weights(self):
        low = number(self.minimum, "Minimum efficiency", positive=True)
        high = number(self.maximum, "Maximum efficiency", positive=True)
        center = number(self.estimate, "Estimated efficiency", positive=True)
        if not low <= center <= high <= 1:
            raise ValueError("Require 0 < minimum <= estimated efficiency <= maximum <= 1")
        if (not isinstance(self.half_weight_pp, Mapping) or not isinstance(self.certainty, str) or
                self.certainty.lower() not in self.half_weight_pp):
            raise ValueError("Choose a certainty level in half_weight_pp")
        widths = {name: number(value, "Half-weight distance in percentage points", positive=True)
                  for name, value in self.half_weight_pp.items()}
        grid = np.array([level for level in range(1, 101) if low <= level / 100 <= high])
        if not len(grid):
            raise ValueError("The bounds must contain at least one whole-percent efficiency")
        distance = abs(grid - 100 * center)
        width = widths[self.certainty.lower()]
        with np.errstate(over="ignore", under="ignore", invalid="ignore"):
            # Subtract the closest exponent before evaluating so a narrow width stays normalized.
            exponent = -((distance - distance.min()) / width) * ((distance + distance.min()) / width)
            weights = np.exp2(np.where(distance == distance.min(), 0., exponent))
        return dict(zip(grid / 100, weights / weights.sum(), strict=True))


def planner_distribution(planner):
    supplied = planner.weights() if isinstance(planner, PlannerEfficiency) else planner
    _, weights = _levels(None, supplied)
    if any(level <= 0 and weight > 0 for level, weight in weights.items()):
        raise ValueError("The capped Poisson workflow requires positive efficiency for every sampled level")
    return {level: weight for level, weight in weights.items() if weight > 0}


@lru_cache(maxsize=512)
def rank_bounds(n, q, confidence):
    count = np.arange(n + 1)
    logs = np.array([lgamma(i + 1) for i in count])
    masses = np.exp(logs[-1] - logs - logs[::-1] + count * log(q) + (n - count) * log(1 - q))
    cdf = masses.cumsum()
    tail = (1 - confidence) / 2
    lo, hi = np.searchsorted(cdf / cdf[-1], (tail, 1 - tail))
    return lo, hi + 1


def wilson(count, n, confidence=.95):
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    p = count / n
    denominator = 1 + z*z/n
    middle = (p + z*z/(2*n)) / denominator
    half = z * np.sqrt(p*(1-p)/n + z*z/(4*n*n)) / denominator
    return max(0., middle - half), min(1., middle + half)


def finite(value):
    return float(value) if np.isfinite(value) else None


def probability(lower, upper):
    n = len(lower)
    lo, hi = int(lower.sum()), int(upper.sum())
    return {"estimate": lo/n if lo == hi else None, "lower": lo/n, "upper": hi/n,
            "mc95": [wilson(lo, n)[0], wilson(hi, n)[1]]}


def date_from(origin, offset):
    return origin + pd.Timedelta(round(offset), unit="ns") if np.isfinite(offset) else None


def minute_interval(later, before):
    return minute_count(later - before) if pd.notna(later) and pd.notna(before) else None


class CompletionSamples:
    """Each completion is known or occurs after the actual run stop."""

    def __init__(self, observations, origin):
        self.origin = pd.Timestamp(origin)
        self.upper = np.array([pd.Timestamp(done).value - self.origin.value if pd.notna(done) else np.inf
                               for done, _ in observations], dtype=float)
        self.stop = np.array([pd.Timestamp(stop).value - self.origin.value for _, stop in observations], dtype=float)
        self.lower = np.where(np.isfinite(self.upper), self.upper, np.maximum(0., self.stop))
        self.sorted_lower = np.sort(self.lower)
        self.sorted_upper = np.sort(self.upper)
        self.n = len(self.upper)

    def quantile(self, q, confidence=.95):
        n = self.n
        rank = ceil(q * n) - 1
        lo, hi = self.sorted_lower[rank], self.sorted_upper[rank]
        lower_rank, upper_rank = rank_bounds(n, q, confidence)
        time = lambda x: date_from(self.origin, x)
        return {"date": time(hi) if lo == hi else None, "earliest": time(lo), "latest": time(hi),
                "mc_interval": [time(self.sorted_lower[lower_rank - 1]) if lower_rank else self.origin,
                                time(self.sorted_upper[upper_rank - 1]) if upper_rank <= n else None]}

    def on_time_indicators(self, due):
        deadline = pd.Timestamp(due).value - self.origin.value
        known = self.upper <= deadline
        possible = known | (~np.isfinite(self.upper) & (self.stop < deadline) & (deadline >= 0.))
        return known, possible

    def summary(self, due, quantiles=DELIVERY_QUANTILES):
        complete = self.upper[np.isfinite(self.upper)]
        n_complete = len(complete)
        mean = complete.mean() if n_complete else np.inf
        std = float(complete.std(ddof=1) / NS_PER_MINUTE) if n_complete > 1 else None
        return {"runs": self.n, "completed": n_complete, "censored": self.n - n_complete,
                "mean_completion": date_from(self.origin, mean) if n_complete == self.n else None,
                "std_minutes": std if n_complete == self.n else None, "completed_only_mean": date_from(self.origin, mean),
                "completed_only_std_minutes": std, "mean_completion_lower_bound": date_from(self.origin, self.lower.mean()),
                "quantiles": {name: self.quantile(q) for name, q in quantiles.items()},
                "on_time": probability(*self.on_time_indicators(due))}


def mean_interval(values):
    if len(values) < 2:
        return [None, None]
    mean = values.mean()
    half = 1.96 * values.std(ddof=1) / np.sqrt(len(values))
    return [float(mean - half), float(mean + half)]


def paired_effect(later, comparison, threshold):
    """Signed later minus comparison. Censored pairs retain bounds and mass."""
    if later.n != comparison.n or later.origin != comparison.origin:
        raise ValueError("Paired scenarios must have the same length and release timestamp")
    lower = (later.lower - comparison.upper) / NS_PER_MINUTE
    upper = (later.upper - comparison.lower) / NS_PER_MINUTE
    observed = lower[(lower == upper) & np.isfinite(lower)]
    paired = len(observed)
    n = later.n
    lo, hi = np.sort(lower), np.sort(upper)
    intervals = {}
    for name, q in (("median", .50), ("p90", .90)):
        rank = ceil(q*n) - 1
        start, end = rank_bounds(n, q, .95)
        intervals[name] = {"estimate": finite(lo[rank]) if lo[rank] == hi[rank] else None,
                           "lower": finite(lo[rank]), "upper": finite(hi[rank]),
                           "mc95": [finite(lo[start - 1]) if start else None, finite(hi[end - 1]) if end <= n else None]}
    # A finite lower bound equal to the threshold is sufficient only for a genuinely censored later completion.
    strict_above = lambda boundary: (lower > boundary) | ((lower == boundary) & ~np.isfinite(later.upper)
                                                         & np.isfinite(comparison.upper) & (later.stop >= 0.))
    return {"runs": n, "complete_pairs": paired, "censored_pairs": n - paired,
            "mean_minutes": float(observed.mean()) if paired == n else None,
            "mean_complete_pairs_minutes": float(observed.mean()) if paired else None,
            "mean_complete_pairs_mc95": mean_interval(observed),
            "median_minutes": intervals["median"], "p90_minutes": intervals["p90"],
            "positive": probability(strict_above(0.), upper > 0.),
            "material": probability(strict_above(threshold), upper > threshold)}


def on_time_change(accepted, baseline, due):
    alo, ahi = accepted.on_time_indicators(due)
    blo, bhi = baseline.on_time_indicators(due)
    lower = alo.astype(int) - bhi
    upper = ahi.astype(int) - blo
    equal = np.array_equal(lower, upper)
    interval = [mean_interval(lower)[0], mean_interval(upper)[1]]
    return {"estimate": float(lower.mean()) if equal else None, "lower": float(lower.mean()),
            "upper": float(upper.mean()), "mc95": [None if x is None else min(1., max(-1., x)) for x in interval]}
