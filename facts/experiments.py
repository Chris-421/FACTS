"""Deterministic RFQ comparison, stratified Monte Carlo, and delivery distributions."""

from dataclasses import dataclass, replace
from numbers import Integral, Real

import numpy as np
import pandas as pd

from .model import number, records
from .simulation import simulate
from .uncertainty import EfficiencySettings


@dataclass
class RFQComparison:
    baseline: object
    accepted: object
    existing_orders: pd.DataFrame
    stations: pd.DataFrame


def _compare(before, after, column, metrics):
    joined = before[[column, *metrics]].merge(after[[column, *metrics]], on=column,
                                              suffixes=("_baseline", "_rfq"), validate="one_to_one")
    for metric in metrics:
        left, right = joined[f"{metric}_baseline"], joined[f"{metric}_rfq"]
        if metric == "completion_time":
            left, right = pd.to_datetime(left), pd.to_datetime(right)
        elif metric == "tardiness":
            left, right = pd.to_timedelta(left), pd.to_timedelta(right)
        else:
            left, right = pd.to_numeric(left), pd.to_numeric(right)
        joined[f"{metric}_change"] = right - left
    return joined


def compare_rfq(data, initial, rfq, **options):
    baseline = simulate(data, initial, scenario_id="baseline", **options)
    accepted = simulate(data, initial, scenario_id="rfq", rfq=rfq, **options)
    orders = _compare(baseline.orders, accepted.orders, "ID_O", ["completion_time", "tardiness", "waiting_minutes"])
    stations = _compare(baseline.stations, accepted.stations, "ID_WS",
                        ["processing_time", "observed_shift_time", "utilization"])
    return RFQComparison(baseline, accepted, orders, stations)


@dataclass
class MonteCarloResult:
    runs: list
    planner_weights: pd.DataFrame
    orders: pd.DataFrame
    experiments: pd.DataFrame
    conditional_delivery: pd.DataFrame
    weighted_delivery: pd.DataFrame
    conditional_summary: pd.DataFrame
    weighted_summary: pd.DataFrame
    rfq_comparisons: list


def _levels(efficiency_levels, planner_weights):
    if planner_weights is None:
        weights = {}
    else:
        if isinstance(planner_weights, pd.DataFrame):
            rows = records(planner_weights, "Efficiency_Beliefs", ("efficiency_level", "probability_weight"))
            entries = ((row["efficiency_level"], row["probability_weight"]) for row in rows)
        else:
            entries = planner_weights.items() if hasattr(planner_weights, "items") else planner_weights
        weights = {}
        for level, weight in entries:
            level = number(level, "Planner efficiency level")
            if level in weights:
                raise ValueError(f"Duplicate planner efficiency level {level}")
            weights[level] = number(weight, "Planner weight")
        if not np.isclose(sum(weights.values()), 1, rtol=0, atol=1e-9):
            raise ValueError("Planner efficiency weights must sum to one")
    if isinstance(efficiency_levels, Real):
        efficiency_levels = [efficiency_levels]
    levels = list(weights) if efficiency_levels is None else list(efficiency_levels)
    levels = [number(level, "Efficiency level") for level in levels]
    if not levels or len(set(levels)) != len(levels) or any(level > 1 for level in levels):
        raise ValueError("Supply distinct efficiency levels between zero and one")
    if weights.keys() - set(levels):
        raise ValueError("Every planner-weighted level must appear in the efficiency grid")
    return levels, weights


def sample_efficiency_trials(planner_weights, runs, seed):
    """Draw a planner efficiency and an independent DES seed per trajectory."""
    _, weights = _levels(None, planner_weights)
    belief_seed, run_seed = np.random.SeedSequence(seed).spawn(2)
    probabilities = np.array(list(weights.values()))
    levels = np.random.default_rng(belief_seed).choice(list(weights), size=runs, p=probabilities / probabilities.sum())
    seeds = run_seed.generate_state(runs, dtype=np.uint64)
    return [(float(level), int(value)) for level, value in zip(levels, seeds, strict=True)]


def monte_carlo(data, initial, *, runs, efficiency_levels=None, planner_weights=None, efficiency_settings=None,
                risk=None, rfq=None, compare=False, seed=0, **options):
    """Run explicit efficiency strata or an operational-risk experiment."""
    if risk is None:
        levels, weights = _levels(efficiency_levels, planner_weights)
    else:
        if efficiency_levels is not None or planner_weights is not None or efficiency_settings is not None:
            raise ValueError("Run generic efficiency and operational risk separately")
        levels, weights = [None], {}
    if compare and rfq is None:
        raise ValueError("A paired scenario comparison requires an RFQ")
    all_runs, comparisons, order_frames, run_frames = [], [], [], []
    run_id = 0
    for level_index, level in enumerate(levels):
        count = runs if isinstance(runs, Integral) else runs[level]
        if isinstance(count, bool) or not isinstance(count, Integral) or count <= 0:
            raise ValueError("Simulation count must be a positive integer for each level")
        for iteration in range(1, count + 1):
            run_id += 1
            run_seed = int(np.random.SeedSequence([seed, level_index, iteration]).generate_state(1, dtype=np.uint64)[0])
            efficiency = None
            if risk is None:
                efficiency = EfficiencySettings(level) if efficiency_settings is None else replace(efficiency_settings, level=level)
            parameters = dict(seed=run_seed, run_id=run_id, efficiency=efficiency, risk=risk, **options)
            if compare:
                result = compare_rfq(data, initial, rfq, **parameters)
                comparisons.append(result)
                replication = [result.baseline, result.accepted]
            else:
                replication = [simulate(data, initial, rfq=rfq, **parameters)]
            for run in replication:
                run.configuration.update(experiment_seed=seed, planner_weight=weights.get(level),
                                         efficiency_grid=tuple(levels), planner_weights=dict(weights))
                run.experiment["experiment_seed"] = seed
                run.experiment["planner_weight"] = weights.get(level)
                all_runs.append(run)
                run_frames.append(run.experiment)
                row = run.experiment.iloc[0]
                order_frames.append(run.orders.assign(efficiency_level=level, conditional_mass=1 / count,
                                                      planner_weight=weights.get(level),
                                                      end_time=row["end_time"], run_status=row["status"]))
    observations = pd.concat(order_frames, ignore_index=True)
    observations["completion_time"] = pd.to_datetime(observations["completion_time"])
    conditional = observations.assign(probability_mass=observations["conditional_mass"])
    weighted = observations.loc[observations["planner_weight"].notna()].copy()
    weighted["probability_mass"] = weighted["conditional_mass"] * weighted["planner_weight"]
    summary_keys = ["scenario_id", "ID_O"]
    return MonteCarloResult(all_runs, pd.DataFrame(list(weights.items()), columns=["efficiency_level", "probability_weight"]),
                            observations, pd.concat(run_frames, ignore_index=True), conditional, weighted,
                            summarize_delivery(conditional, [*summary_keys, "efficiency_level"]),
                            summarize_delivery(weighted, summary_keys), comparisons)


def summarize_delivery(samples, keys):
    metrics = ["completion_probability", "mean_completion_time", "mean_completed_only", "p05", "median", "p90", "p95",
               "p95_p05_spread", "on_time_probability_lower_bound", "on_time_probability_upper_bound",
               "conditional_on_time_probability"]
    rows = []
    for key, group in samples.groupby(keys, dropna=False, sort=False):
        mass = group["probability_mass"].astype(float)
        total = mass.sum()
        if total <= 0:
            continue
        done = group.loc[group["completion_time"].notna() & (mass > 0)].sort_values("completion_time")
        probabilities = done["probability_mass"].astype(float).to_numpy() / total
        cdf = np.cumsum(probabilities)
        completed = min(cdf[-1], 1.0) if len(cdf) else 0.0

        def quantile(probability):
            index = np.searchsorted(cdf + 1e-12, probability, side="left")
            return done["completion_time"].iloc[index] if index < len(done) else pd.NaT

        mean = pd.NaT
        if len(done):
            start = done["completion_time"].iloc[0]
            offsets = (done["completion_time"] - start).dt.total_seconds()
            mean = start + pd.Timedelta(seconds=np.average(offsets, weights=probabilities))
        p05, median, p90, p95 = (quantile(q) for q in (.05, .5, .9, .95))
        on_time = mass.loc[group["completion_time"] <= group["due_date"]].sum() / total
        unresolved = group["completion_time"].isna() & (group["end_time"] < group["due_date"])
        if "run_status" in group:
            unresolved &= group["run_status"] == "limit_reached"
        unknown = mass.loc[unresolved].sum() / total
        known = 1 - unknown
        rows.append((*key, completed, mean if np.isclose(completed, 1, rtol=0, atol=1e-12) else pd.NaT,
                     mean, p05, median, p90, p95, p95 - p05 if pd.notna(p95) and pd.notna(p05) else pd.NaT,
                     on_time, on_time + unknown, on_time / known if known > 0 else np.nan))
    return pd.DataFrame(rows, columns=[*keys, *metrics])
