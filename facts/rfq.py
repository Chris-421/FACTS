"""Integrated FACTS RFQ decision support."""

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from functools import partial
from numbers import Integral

import numpy as np
import pandas as pd

from .calendar import timestamp
from .experiments import sample_efficiency_trials
from .flow import FlowObserver, summarize_capacity
from .forecast import (CompletionSamples, PlannerEfficiency, minute_interval, on_time_change, paired_effect,
                       planner_distribution, probability)
from .inventory import InventoryRules
from .model import Model, minute_count, number
from .poisson import ParetoDowntime, PoissonEfficiencySettings
from .rfq_risk import analyze_operational_risk, order_dates
from .simulation import RunLimits, run_model
from .uncertainty import RiskSettings


@dataclass(frozen=True)
class RFQSettings:
    until: object
    runs: int = 2000
    risk_runs: int = 500
    seed: int = 0
    commitment_probability: float = .90
    dispatch_cutoff: str | time | None = None
    idle_policy: str = "immediate"
    practical_delay_minutes: float = 60.
    critical_probability: float = .20
    min_complete_pairs: int = 30
    min_complete_fraction: float = .80
    max_interaction_pairs: int = 3
    max_events: int = 20_000

    def __post_init__(self):
        for key in ("runs", "risk_runs", "seed", "min_complete_pairs", "max_interaction_pairs", "max_events"):
            value = getattr(self, key)
            minimum = 0 if key in ("seed", "max_interaction_pairs") else 1
            if isinstance(value, bool) or not isinstance(value, Integral) or value < minimum:
                raise ValueError(f"{key} must be an integer of at least {minimum}")
        if not 0 < number(self.commitment_probability, "Commitment probability") < 1:
            raise ValueError("Commitment probability must be strictly between zero and one")
        for key in ("critical_probability", "min_complete_fraction"):
            if not 0 < number(getattr(self, key), key) <= 1:
                raise ValueError(f"{key} must be greater than zero and at most one")
        number(self.practical_delay_minutes, "Practical delay in minutes")
        if self.idle_policy not in ("immediate", "next_assignment"):
            raise ValueError("Choose immediate or next_assignment")
        parse_cutoff(self.dispatch_cutoff)


@dataclass
class RFQDecision:
    report: dict
    traces: dict[str, pd.DataFrame]


def parse_cutoff(value):
    if value is None:
        return None
    clock = time.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(clock, time) or clock.tzinfo is not None:
        raise ValueError("Dispatch cutoff must be a factory-local clock time")
    return clock


def practical_dispatch(calendar, ready, cutoff):
    """Earliest working instant on or after production, before the daily cutoff."""
    first = ready.date() - timedelta(days=1)
    if calendar.recurs:
        last = max(ready.date(), max(calendar.exceptions, default=ready.date())) + timedelta(days=7)
        dates = (first + timedelta(days=offset) for offset in range((last - first).days + 1))
    else:
        dates = sorted(day for day in calendar.exceptions if day >= first)
    for day in dates:
        for shift in calendar.on_date(day):
            when = max(ready, shift.t_start)
            if when > shift.t_done:
                continue
            if cutoff is None or when.time() <= cutoff:
                return when
            tomorrow = pd.Timestamp(datetime.combine(when.date() + timedelta(days=1), time.min))
            if tomorrow <= shift.t_done:
                return tomorrow
    return None


def pareto_envelope(scenarios):
    ranges = {}
    for name in ("p50", "p80", "p90", "p95"):
        values = [entry["forecast"]["quantiles"][name]["date"] for entry in scenarios.values()]
        ranges[name] = {"lower": min(values), "upper": max(values)} if all(value is not None for value in values) else None
    on_time = [entry["forecast"]["on_time"] for entry in scenarios.values()]
    ranges["on_time"] = {"lower": min(value["lower"] for value in on_time), "upper": max(value["upper"] for value in on_time),
                         "all_shapes_identified": all(value["estimate"] is not None for value in on_time)}
    return ranges


def order_impact(pairs, model, settings):
    orders = []
    for key, (before, after) in pairs.items():
        order = model.orders[key]
        baseline = CompletionSamples(before, order.RD)
        accepted = CompletionSamples(after, order.RD)
        paired = paired_effect(accepted, baseline, settings.practical_delay_minutes)
        left, right = baseline.quantile(.90, confidence=.975), accepted.quantile(.90, confidence=.975)
        marginal = minute_interval(right["date"], left["date"])
        interval = [minute_interval(right["mc_interval"][0], left["mc_interval"][1]),
                    minute_interval(right["mc_interval"][1], left["mc_interval"][0])]
        orders.append({"order": key, "due": order.DD, **paired, "baseline_p90": left["date"], "accepted_p90": right["date"],
                       "marginal_p90_change_minutes": marginal, "marginal_p90_change_mc95": interval,
                       "baseline_on_time": probability(*baseline.on_time_indicators(order.DD)),
                       "accepted_on_time": probability(*accepted.on_time_indicators(order.DD)),
                       "on_time_probability_change": on_time_change(accepted, baseline, order.DD),
                       "miss_probability_change": on_time_change(baseline, accepted, order.DD),
                       "material_delay_flag": paired["material"]["lower"] >= settings.critical_probability})
    orders.sort(key=lambda row: (row["material_delay_flag"], row["material"]["lower"]), reverse=True)
    return {"orders": orders, "materially_delayed_orders": sum(row["material_delay_flag"] for row in orders)}


def assess_rfq(data, initial, rfq, *, planner, settings, risk=None, inventory=None):
    """Return an RFQ decision and separate trajectory and exposure tables."""
    if len(rfq) != 1:
        raise ValueError("Supply one prospective RFQ order with one or more jobs per assessment")
    if not isinstance(settings, RFQSettings):
        raise ValueError("Supply RFQSettings with a documented observation cutoff")
    target = rfq["ID_O"].iloc[0]
    weights = planner_distribution(planner)
    before = Model(data, initial, None)
    accepted = Model(data, initial, rfq)
    order = accepted.orders[target]
    end = timestamp(settings.until)
    if end <= max(accepted.start, order.RD):
        raise ValueError("The forecast horizon must extend past both snapshot and RFQ release")
    risk = risk or RiskSettings(end)
    if risk.controlled_stations or risk.controlled_suppliers:
        raise ValueError("Supply the full operational-risk configuration to assess_rfq")
    if not accepted.start < timestamp(risk.observation_end) <= end:
        raise ValueError("Risk exposure must end after the snapshot and at or before the forecast cutoff")
    limits = RunLimits(end, int(settings.max_events))
    inventory = inventory or InventoryRules()
    run = partial(run_model, inventory=inventory, limits=limits)

    def run_accepted(**opts):
        return run(accepted.fork(), [target], **opts)

    traces = defaultdict(list)
    states = Counter()
    pairs = {key: ([], []) for key in before.orders}
    trials = sample_efficiency_trials(weights, settings.runs, [int(settings.seed), 0])
    laws = {name: ParetoDowntime(top_share=share) for name, share in (("80/20", .80), ("70/30", .70), ("90/10", .90))}
    samples = {}
    scenarios = {}
    for name, law in laws.items():
        outcomes = []
        for trial, (efficiency, seed) in enumerate(trials, 1):
            generic = PoissonEfficiencySettings(efficiency, law, settings.idle_policy)
            flow = FlowObserver(target) if name == "80/20" else None
            actual = run_accepted(efficiency=generic, seed=seed, run_id=trial, observer=flow)
            actual_state = actual.experiment.iloc[0]
            dates = order_dates(actual)
            outcome = dates[target], actual_state.end_time
            outcomes.append(outcome)
            states[(name, actual_state.status)] += 1
            traces["efficiency_outcomes"].append({"trial": trial, "seed": seed, "law": name, "efficiency": efficiency,
                                                  "completion": outcome[0], "end": outcome[1], "status": actual_state.status})
            for job in actual.jobs.itertuples(index=False):
                if job.ID_O == target:
                    traces["rfq_jobs"].append({"trial": trial, "law": name, "job": job.ID_J,
                                               "completion": job.completion_time, "state": job.state})
            if flow is None:
                continue
            baseline = run(before.fork(), [], efficiency=generic, seed=seed, run_id=trial)
            baseline_dates = order_dates(baseline)
            stop = baseline.experiment.iloc[0].end_time
            for key, (left, right) in pairs.items():
                left.append((baseline_dates[key], stop))
                right.append((dates[key], outcome[1]))
                traces["existing_order_pairs"].append({"trial": trial, "seed": seed, "efficiency": efficiency, "order": key,
                                                       "baseline_completion": baseline_dates[key], "baseline_end": stop,
                                                       "accepted_completion": dates[key], "accepted_end": outcome[1],
                                                       "paired_change_minutes": minute_interval(dates[key], baseline_dates[key])})
            traces["capacity_paths"].extend({"trial": trial, **row} for row in flow.station_and_group_rows(actual, accepted.stations))
        samples[name] = CompletionSamples(outcomes, order.RD)
        scenarios[name] = {"shape": law.shape, "underlying_top_20_share": law.top_share, "minimum_minutes": law.minimum,
                           "elementary_cap_minutes": law.maximum, "expected_elementary_minutes": law.mean,
                           "forecast": samples[name].summary(order.DD)}
    central = samples["80/20"]
    quantile = central.quantile(settings.commitment_probability)
    cutoff = parse_cutoff(settings.dispatch_cutoff)
    production = quantile["date"]
    quoted = practical_dispatch(accepted.calendar, production, cutoff) if production is not None else None
    basis = None if quoted is None else ("modeled dispatch" if cutoff is not None else "production completion")
    commitment = {"production_quantile": quantile, "recommended_production_completion": production,
                  "recommended_commitment": quoted, "commitment_basis": basis,
                  "threshold": settings.commitment_probability, "transportation_modeled": False,
                  "customer_requested_date": order.DD, "forecast": scenarios["80/20"]["forecast"]}
    if cutoff is not None:
        completions = [practical_dispatch(accepted.calendar, central.origin + pd.Timedelta(round(value), unit="ns"), cutoff)
                       if np.isfinite(value) else None for value in central.upper]
        known = np.array([value is not None and value <= order.DD for value in completions])
        possible = known | (~np.isfinite(central.upper) & central.on_time_indicators(order.DD)[1])
        commitment["dispatch_on_time"] = probability(known, possible)
    capacity_rows = summarize_capacity(pd.DataFrame(traces["capacity_paths"]))
    groups = [row for row in capacity_rows if row["resource_type"] == "group"]
    exposed_rfq = sorted((row for row in groups if row["mean_rfq_capacity_wait_minutes"] > 0.),
                         key=lambda row: row["mean_rfq_capacity_wait_minutes"], reverse=True)
    pressured = sorted((row for row in groups if row["mean_capacity_wait_job_minutes"] > 0.),
                       key=lambda row: row["mean_capacity_wait_job_minutes"], reverse=True)
    seeds = np.random.SeedSequence([int(settings.seed), 1]).generate_state(settings.risk_runs, dtype=np.uint64)
    risks = analyze_operational_risk(run_accepted, accepted, target, risk, settings, seeds, traces,
                                    sample_otp=inventory.sample_otp)
    return RFQDecision({
        "schema_version": 1, "rfq_id": target, "released_at": order.RD, "settings": asdict(settings),
        "planner": asdict(planner) if isinstance(planner, PlannerEfficiency) else {"source": "explicit weights"},
        "efficiency_probabilities": [{"efficiency": level, "probability": value} for level, value in weights.items()],
        "sampled_efficiencies": [{"efficiency": level, "count": count} for level, count in sorted(Counter(x[0] for x in trials).items())],
        "commitment": commitment, "pareto_sensitivity": {"central": "80/20", "assumptions": scenarios,
                                                        "ranges": pareto_envelope(scenarios)},
        "capacity": {"resources": capacity_rows, "main_factory_constraint": pressured[0]["resource"] if pressured else None,
                     "main_rfq_constraint": exposed_rfq[0]["resource"] if exposed_rfq else None},
        "operational_risk": risks, "existing_order_impact": order_impact(pairs, before, settings),
        "run_status_counts": [{"law": law, "status": status, "count": count} for (law, status), count in states.items()],
    }, {name: pd.DataFrame(rows) for name, rows in traces.items()})
