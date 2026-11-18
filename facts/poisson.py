"""Operation-completion Poisson losses, promoted from the frozen V2 study."""

from dataclasses import dataclass, field
from math import expm1, isfinite, log

import numpy as np

from .events import Event
from .model import minute_count, minutes, number


@dataclass(frozen=True)
class ParetoDowntime:
    minimum: float = 1.0
    top_share: float = .80
    maximum: float | None = 480.0

    def __post_init__(self):
        minimum = number(self.minimum, "Pareto minimum", positive=True)
        if not .2 < number(self.top_share, "Top 20% downtime share") < 1.:
            raise ValueError("The underlying top 20% share must be strictly between 20% and 100%")
        if self.maximum is not None and number(self.maximum, "Elementary cap") < minimum:
            raise ValueError("The elementary cap must be at least the Pareto minimum")

    @property
    def shape(self):
        return 1 / (1 - log(self.top_share) / log(.2))

    @property
    def mean(self):
        alpha = self.shape
        if self.maximum is None:
            return self.minimum * alpha / (alpha - 1)
        return self.minimum * (1 - expm1((alpha - 1) * log(self.minimum / self.maximum)) / (alpha - 1))

    def sample(self, rng, count):
        values = self.minimum * (1 + rng.pareto(self.shape, size=count))
        return values if self.maximum is None else np.minimum(values, self.maximum)


@dataclass(frozen=True)
class PoissonEfficiencySettings:
    level: float
    downtime: ParetoDowntime = field(default_factory=ParetoDowntime)
    idle_policy: str = "immediate"

    def __post_init__(self):
        if not 0 < number(self.level, "Poisson efficiency") <= 1:
            raise ValueError("Poisson efficiency must be greater than zero and at most one")
        if not isinstance(self.downtime, ParetoDowntime):
            raise ValueError("Supply a ParetoDowntime configuration")
        if self.idle_policy not in ("immediate", "next_assignment"):
            raise ValueError("Choose immediate or next_assignment for generic outage activation")

    @property
    def rate(self):
        return (1 - self.level) / (self.level * self.downtime.mean)


class WorkingLoss:
    def __init__(self, settings, stations):
        self.settings = settings
        self.rate = settings.rate
        self.started = {}
        self.pending = {}
        self.outstanding = {}
        self.shift_exposure = {}
        self.generated = dict.fromkeys(stations, 0.)

    def schedule_recovery(self, engine, key):
        station = engine.model.stations[key]
        if station.t_done is None and self.outstanding[key] <= minute_count(engine.factory.t_done - engine.now):
            station.t_done = min(engine.factory.t_done, engine.now + minutes(self.outstanding[key], "Generic outage"))
            engine.queue.add(station.t_done, Event.STATION_RECOVERY, key)

    def begin_shift(self, engine):
        for key, remaining in self.outstanding.items():
            self.shift_exposure[key] = engine.now, remaining
            self.schedule_recovery(engine, key)

    def advance(self, when):
        # Measure from the shift opening or activation, independently of intervening events.
        for key, (start, remaining) in self.shift_exposure.items():
            self.outstanding[key] = max(0., remaining - minute_count(when - start))

    def recover(self, key):
        self.outstanding.pop(key, None)
        self.shift_exposure.pop(key, None)

    def finish(self, engine, station, rng):
        key = station.ID_WS
        productive = minute_count(engine.now - self.started.pop(key))
        if not self.rate or not productive:
            return
        count = int(rng.poisson(self.rate * productive))
        engine.journal.draw(engine.now, "station", key, "generic_poisson_count", count,
                            operating_minutes=productive, rate_per_minute=self.rate)
        if not count:
            return
        sampled = self.settings.downtime.sample(rng, count)
        total = float(sampled.sum())
        if not isfinite(total):
            raise ValueError("Sampled generic downtime exceeds the finite numeric range")
        episode = total, tuple(float(item) for item in sampled)
        self.generated[key] += total
        if self.settings.idle_policy == "next_assignment":
            self.pending[key] = episode
            engine.journal.event(engine.now, "station", key, "generic_unavailability_pending", duration_minutes=total,
                                 interruption_count=count)
        else:
            self.activate(engine, station, episode)

    def activate(self, engine, station, episode):
        if station.state != "available":
            raise RuntimeError("Generic unavailability requires an available station")
        total, draws = episode
        key = station.ID_WS
        self.outstanding[key] = total
        station.state, station.reason = "unavailable", "GENERIC_EFFICIENCY"
        if engine.factory.active:
            self.shift_exposure[key] = engine.now, total
            self.schedule_recovery(engine, key)
        engine.journal.event(engine.now, "station", key, "unavailability_start", "available", "unavailable",
                             reason=station.reason, duration_minutes=total, underlying_minutes=draws,
                             interruption_count=len(draws), time_basis="working", t_done=station.t_done)
