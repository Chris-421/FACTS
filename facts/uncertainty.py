"""Mode-specific draws kept outside the shared station state machine."""

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import isfinite

import numpy as np
import pandas as pd

from .calendar import timestamp
from .events import Event
from .model import Parameter, minute_count, minutes, missing, number, sequence
from .poisson import PoissonEfficiencySettings, WorkingLoss
from .scheduling import ModelingDecisionRequired


@dataclass(frozen=True)
class EfficiencySettings:
    level: float
    short_budget: str = "cap"
    pareto_minimum: float = 5
    pareto_shape: float = 1.161
    initial_remaining: Mapping | Callable | None = None
    sample_duration: Callable | None = None


@dataclass(frozen=True)
class RiskSettings:
    observation_end: object
    reliability: Mapping | None = None
    controlled_stations: frozenset = frozenset()
    controlled_suppliers: frozenset = frozenset()
    matched_supply_streams: bool = False


@dataclass(frozen=True)
class MachineReliability:
    tbf: object
    ttr: object
    initial_tbf_remaining: object


@dataclass(frozen=True)
class FailureCycle:
    operating_time: pd.Timedelta
    repair_time: pd.Timedelta
    number: int


class MachineClock:
    def __init__(self, station, terms, timing_rng, repair_rng):
        self.station = station
        self.terms = terms
        self.timing_rng = timing_rng
        self.repair_rng = repair_rng
        self.cycle_number = 0
        self.running_total = pd.Timedelta(0)
        self.remaining = None
        self.current = None
        self.failed = False
        self.planned = deque()

    @staticmethod
    def _draw(parameter, rng, name):
        if isinstance(parameter, Parameter):
            parameter = parameter.value(rng)
        elif callable(parameter):
            parameter = parameter(rng)
        return minutes(parameter, name)

    def sample(self, engine, observation_limit):
        self.cycle_number += 1
        first = self.cycle_number == 1
        threshold = self.terms.initial_tbf_remaining if first else self.terms.tbf
        operating = self._draw(threshold, self.timing_rng, f"Time to failure for {self.station!r}")
        if not first and operating <= pd.Timedelta(0):
            raise ValueError(f"Successive failures require positive operating time for {self.station!r}")
        repair = self._draw(self.terms.ttr, self.repair_rng, f"Repair time for {self.station!r}")
        self.running_total += operating
        details = {"cycle": self.cycle_number, "reference_operating_minute": minute_count(self.running_total),
                   "exposure_eligible": self.running_total <= observation_limit, "exogenous": True}
        engine.journal.draw(engine.now, "station", self.station, "tbf_minutes", minute_count(operating), **details)
        engine.journal.draw(engine.now, "station", self.station, "repair_minutes", minute_count(repair), **details)
        return FailureCycle(operating, repair, self.cycle_number)

    def start_cycle(self, engine, observation_limit):
        self.current = self.planned.popleft() if self.planned else self.sample(engine, observation_limit)
        self.remaining = self.current.operating_time


def downtime_probability(budget, remaining):
    return float(np.clip(budget / remaining, 0, 1)) if remaining > 0 else 0.0


def machine_reliability(model, risk):
    configured = dict(risk.reliability or {})
    unknown = configured.keys() - model.stations.keys()
    if unknown:
        raise ValueError(f"Reliability supplied for unknown stations: {unknown}")
    result = {}
    for key, station in model.station_data.items():
        terms = configured.get(key)
        fields = [station.get(name) for name in ("tbf", "ttr", "initial_tbf_remaining")]
        if terms is None and not all(missing(field) for field in fields):
            terms = MachineReliability(*fields)
        if terms is None:
            continue
        if not isinstance(terms, MachineReliability):
            raise ValueError(f"Reliability for {key!r} must be a MachineReliability")
        if any(missing(value) for value in (terms.tbf, terms.ttr, terms.initial_tbf_remaining)):
            raise ValueError(f"Supply TBF, TTR, and initial remaining operating time for {key!r}")
        result[key] = terms
    return result


def supplier_lead_times(model):
    return {(material, supplier): terms["DLT"][supplier] if isinstance(terms["DLT"], Mapping) else terms["DLT"]
            for material, terms in model.materials.items() for supplier in sequence(terms["suppliers"], "Suppliers")}


class Uncertainty:
    def __init__(self, model, seed, efficiency, risk):
        self.efficiency = efficiency
        self.risk = risk
        self.budget = {}
        self.evaluated = {}
        self.exposure_end = None
        self.working_loss = WorkingLoss(efficiency, model.stations) if isinstance(efficiency, PoissonEfficiencySettings) else None
        station_seed, material_seed = np.random.SeedSequence(seed).spawn(2)
        self.stations = {key: np.random.default_rng(stream)
                         for key, stream in zip(model.stations, station_seed.spawn(len(model.stations)))}
        material_streams = {key: stream.spawn(2)
                            for key, stream in zip(model.materials, material_seed.spawn(len(model.materials)))}
        self.materials = {key: np.random.default_rng(streams[0]) for key, streams in material_streams.items()}
        self.intervals = {key: np.random.default_rng(streams[1]) for key, streams in material_streams.items()}
        self.supplier_streams = {}
        self.clocks = {}
        if efficiency is not None:
            self.level = number(efficiency.level, "Efficiency")
            if self.level > 1:
                raise ValueError("Efficiency must be between zero and one")
            if self.working_loss is None:
                self.minimum = number(efficiency.pareto_minimum, "Pareto minimum", positive=True)
                self.shape = number(efficiency.pareto_shape, "Pareto shape", positive=True)
                if efficiency.short_budget not in ("cap", "skip"):
                    raise ValueError("short_budget must be 'cap' or 'skip'")
            self.evaluation_level = self.level
        else:
            self.level = 1.0
            self.evaluation_level = None
        if risk is not None:
            self.exposure_end = timestamp(risk.observation_end)
            if self.exposure_end <= model.start:
                raise ValueError("The operational risk observation window must end after the snapshot")
            self.reference_operating_time = model.calendar.shift_time_between(model.start, self.exposure_end)
            configured = machine_reliability(model, risk)
            if set(risk.controlled_stations) - configured.keys():
                raise ValueError("A controlled station must have configured reliability")
            if risk.controlled_suppliers and set(risk.controlled_suppliers) - supplier_lead_times(model).keys():
                raise ValueError("A controlled supplier must identify a configured material and supplier")
            if risk.matched_supply_streams:
                for key, terms in model.materials.items():
                    suppliers = sequence(terms["suppliers"], "Suppliers")
                    self.supplier_streams.update(
                        ((key, supplier), np.random.default_rng(stream))
                        for supplier, stream in zip(suppliers, material_streams[key][0].spawn(len(suppliers)), strict=True))
            for key, terms in configured.items():
                if key in risk.controlled_stations:
                    continue
                timing, repair = self.stations[key].spawn(2)
                self.clocks[key] = MachineClock(key, terms, timing, repair)

    def initialize(self, engine):
        if self.working_loss is not None:
            self.working_loss.started.update((key, engine.now) for key, station in engine.model.stations.items()
                                             if station.state == "processing")
        for key, clock in self.clocks.items():
            limit = self.reference_operating_time
            while clock.running_total <= limit:
                if clock.cycle_number >= engine.limits.max_events:
                    raise ValueError(f"Risk sampling exceeded the cycle limit for {key!r}")
                clock.planned.append(clock.sample(engine, limit))
            clock.start_cycle(engine, limit)
            station = engine.model.stations[key]
            if station.state == "processing":
                self.run_operation(engine, key, station.t_done)
            elif station.state == "available" and clock.remaining == pd.Timedelta(0):
                self.run_operation(engine, key, engine.now)

    def run_operation(self, engine, key, completion):
        if self.working_loss is not None:
            self.working_loss.started[key] = engine.now
        if key not in self.clocks:
            return
        clock = self.clocks[key]
        duration = completion - engine.now
        if duration >= clock.remaining:
            due = engine.now + clock.remaining
            engine.queue.add(completion, Event.MACHINE_FAILURE, (key, clock.current, due))

    def advance_operation(self, station, elapsed):
        clock = self.clocks.get(station)
        if clock is not None:
            clock.remaining = max(pd.Timedelta(0), clock.remaining - elapsed)

    def recover(self, engine, station):
        if self.working_loss is not None:
            self.working_loss.recover(station.ID_WS)
        clock = self.clocks.get(station.ID_WS)
        if clock is None:
            return
        if clock.failed:
            clock.failed = False
            clock.start_cycle(engine, self.reference_operating_time)
        elif clock.remaining == pd.Timedelta(0):
            self.run_operation(engine, station.ID_WS, engine.now)

    def begin_shift(self, engine, *, partial):
        if self.working_loss is not None:
            self.budget.update(dict.fromkeys(engine.model.stations, None))
            return None
        shift = engine.factory.shift
        target = (shift.t_done - shift.t_start) * (1 - self.level)
        self.evaluated.clear()
        for key in engine.model.stations:
            available = target
            if partial and target > pd.Timedelta(0):
                policy = self.efficiency.initial_remaining
                if policy is None:
                    # Resolve lazily if this station first has an eligible assignment.
                    available = None
                else:
                    supplied = policy(key, shift, engine.now, self.level) if callable(policy) else policy.get(key)
                    if supplied is None:
                        raise ValueError(f"Missing initial efficiency budget for station {key!r}")
                    available = minutes(supplied, f"Initial efficiency budget for {key!r}")
                    if available > target:
                        raise ValueError(f"Initial efficiency budget exceeds the shift target for {key!r}")
            self.budget[key] = available
        return target

    def generic_duration(self, engine, station_id):
        if self.efficiency is None or self.working_loss is not None or self.evaluated.get(station_id) == engine.now:
            return None
        self.evaluated[station_id] = engine.now
        remaining = engine.factory.t_done - engine.now
        budget = self.budget[station_id]
        if budget is None:
            raise ModelingDecisionRequired("Supply the remaining loss budget when starting partway through a shift")
        probability = downtime_probability(minute_count(budget), minute_count(remaining))
        if probability == 0:
            return None
        rng = self.stations[station_id]
        draw = rng.random()
        engine.journal.draw(engine.now, "station", station_id, "generic_trigger", draw, probability=probability)
        if draw >= probability:
            return None
        cap = min(budget, remaining)
        if minute_count(cap) < self.minimum:
            if self.efficiency.short_budget == "skip":
                return None
        sample = self.efficiency.sample_duration
        value = self.minimum * (1 + rng.pareto(self.shape)) if sample is None else sample(rng)
        if not isinstance(value, (int, float, np.number)) or np.isnan(value) or value < self.minimum:
            raise ValueError("Generic downtime sampler returned an invalid value or one below the minimum")
        duration = min(cap, minutes(value, "Generic downtime")) if isfinite(value) and value < minute_count(cap) else cap
        engine.journal.draw(engine.now, "station", station_id, "generic_duration_minutes", value,
                            applied_minutes=minute_count(duration))
        self.budget[station_id] -= duration
        return duration if duration > pd.Timedelta(0) else None

    def lead_time(self, engine, material, supplier, purchase_id, terms, nominal):
        key = material, supplier
        if (self.risk is None or key in self.risk.controlled_suppliers or
                not isinstance(terms, Parameter) or terms.sampler is None):
            return nominal
        stream = self.supplier_streams.get(key, self.materials[material])
        value = number(terms.value(stream), f"Sampled DLT for {material!r}")
        engine.journal.draw(engine.now, "supplier", supplier, "lead_time_minutes", value,
                            material=material, purchase_id=purchase_id, delay_minutes=value - nominal,
                            arrival=engine.now + minutes(value, "Sampled DLT"),
                            exposure_eligible=engine.now < self.exposure_end)
        return value

    def order_interval(self, engine, material, terms, sample):
        if isinstance(terms, Parameter):
            if sample and self.risk is not None and terms.sampler is not None:
                interval = number(terms.value(self.intervals[material]), f"Sampled OTP for {material!r}", positive=True)
                engine.journal.draw(engine.now, "inventory", material, "order_interval_minutes", interval)
                return interval
            terms = terms.nominal
        return number(terms, f"Ordering interval for {material!r}", positive=True)

    def failure(self, engine, key, cycle, due):
        station = engine.model.stations[key]
        self.clocks[key].failed = True
        engine.journal.draw(engine.now, "station", key, "failure_start", engine.now, exogenous=False,
                            due_at=due, deferred_minutes=minute_count(engine.now - due), cycle=cycle.number)
        engine.start_unavailability(station, cycle.repair_time, "MACHINE_FAILURE",
                                    due_at=due, deferred_minutes=minute_count(engine.now - due), cycle=cycle.number)
