"""RFQ risk exposures and matched machine and supply counterfactuals."""

from collections import defaultdict
from dataclasses import replace
from itertools import combinations, islice

import numpy as np
import pandas as pd

from .flow import FlowObserver
from .forecast import CompletionSamples, NS_PER_MINUTE, mean_interval, paired_effect
from .inventory import Purchasing
from .model import Parameter, minute_count
from .sensitivity import association
from .uncertainty import machine_reliability, supplier_lead_times


def order_dates(run):
    return dict(zip(run.orders.ID_O, run.orders.completion_time, strict=True))


def observation(run, order_id):
    return order_dates(run)[order_id], run.experiment.iloc[0].end_time


def descriptive(data):
    observed = pd.Series(data, dtype=float).dropna().to_numpy()
    if not len(observed):
        return {"n": 0, "mean": None, "median": None, "p90": None, "max": None}
    ordered = np.sort(observed)
    return {"n": len(observed), "mean": float(observed.mean()), "median": float(ordered[int(np.ceil(len(ordered)*.5))-1]),
            "p90": float(ordered[int(np.ceil(len(ordered)*.9))-1]), "max": float(ordered[-1])}


def record_exposures(run, flow, model, target, stop, materials, entities, trial, traces):
    """Keep per-incident records and per-run exposure features with explicit time windows."""
    events = list(run.event_log.itertuples(index=False))
    deliveries = {(e.entity_id, e.related_entity_id): e for e in events if e.event_type == "material_delivery"}
    orders = {(e.entity_id, e.related_entity_id): e for e in events if e.event_type == "material_order"}
    requirements = defaultdict(list)
    for row in flow.requirements:
        requirements[row["material"]].append(row)
        traces["material_requirements"].append({"trial": trial, "kind": "queue_interval", **row})
    ready = {(row.ID_J, row.step): row.ready_at for row in run.initial["J"].itertuples(index=False)
             if row.ID_O == target}
    operation_waits = defaultdict(int)
    for event in events:
        if event.entity_type == "job" and event.entity_id[0] == target:
            if event.event_type == "operation_complete" and event.details["next_step"] is not None:
                ready[event.entity_id, event.details["next_step"]] = event.timestamp
            elif event.event_type == "operation_start":
                step = event.details["step"]
                entered = ready.get((event.entity_id, step))
                if pd.isna(entered):
                    entered = model.orders[target].RD
                working_wait = minute_count(model.calendar.shift_time_between(entered, event.timestamp))
                if event.timestamp <= stop:
                    operation_waits[event.related_entity_id] += working_wait
                traces["operations"].append({"trial": trial, "job": event.entity_id, "step": step,
                                            "station": event.related_entity_id, "ready_at": entered,
                                            "started_at": event.timestamp, "expected_finish": event.details["t_done"],
                                            "calendar_wait_minutes": minute_count(event.timestamp - entered),
                                            "working_wait_minutes": working_wait})
        if event.event_type == "material_consumption" and event.related_entity_id[0] == target:
            step = event.details["step"]
            entered = ready.get((event.related_entity_id, step))
            traces["material_requirements"].append({"trial": trial, "kind": "consumption", "job": event.related_entity_id,
                                                      "step": step, "material": event.entity_id,
                                                      "required_at": model.orders[target].RD if pd.isna(entered) else entered,
                                                      "consumed_at": event.timestamp, "stock": event.state_before,
                                                      "quantity_required": event.details["q"], "stock_after": event.state_after})
    episodes = defaultdict(list)
    for event in events:
        if event.event_type != "unavailability_start" or event.details["reason"] != "MACHINE_FAILURE":
            continue
        recovery = event.details["t_done"]
        working = minute_count(model.calendar.shift_time_between(event.timestamp, min(recovery, stop))) if event.timestamp < stop else 0.
        row = {"trial": trial, "station": event.entity_id, "failure_at": event.timestamp, "scheduled_recovery": recovery,
               "due_at": event.details["due_at"], "cycle": event.details["cycle"], "repair_calendar_minutes": event.details["duration_minutes"],
               "working_downtime_in_rfq_window_minutes": working, "within_rfq_window": event.timestamp < stop}
        traces["machine_episodes"].append(row)
        if row["within_rfq_window"]:
            episodes[event.entity_id].append(working)
    for draw in run.sampled_inputs.itertuples(index=False):
        if draw.parameter in ("tbf_minutes", "repair_minutes"):
            traces["machine_samples"].append({"trial": trial, "station": draw.entity_id, "sampled_at": draw.timestamp,
                                               "parameter": draw.parameter, "minutes": draw.sampled_value, **draw.details})
    sampled_dlt = defaultdict(list)
    arrivals_into_shortage = defaultdict(int)
    for (material, purchase), event in orders.items():
        detail = event.details
        supplier = detail["supplier"]
        actual = deliveries.get((material, purchase))
        nominal = Purchasing.nominal(materials[material, supplier])
        arrival = None if actual is None else actual.timestamp
        waiting = [row for row in requirements[material] if row["short_wait_working_minutes"] and arrival is not None
                   and row["from"] < arrival <= row["until"]]
        row = {"trial": trial, "material": material, "supplier": supplier, "purchase_order": purchase,
               "order_at": event.timestamp, "nominal_dlt_minutes": nominal,
               "sampled_dlt_minutes": detail["lead_time_minutes"], "scheduled_arrival": detail["arrival"],
               "actual_arrival": arrival, "stock_before_delivery": None if actual is None else actual.state_before,
               "stock_after_delivery": None if actual is None else actual.state_after,
               "rfq_jobs_short_at_arrival": [item["job"] for item in waiting],
               "after_production": detail.get("after_production", False), "placed_before_rfq_window_end": event.timestamp < stop}
        traces["supplier_deliveries"].append(row)
        if event.timestamp < stop:
            sampled_dlt[material, supplier].append(float(detail["lead_time_minutes"]))
        if waiting and arrival <= stop:
            arrivals_into_shortage[material, supplier] += 1
    for (material, purchase), event in deliveries.items():
        if (material, purchase) not in orders:
            traces["supplier_deliveries"].append({"trial": trial, "material": material, "supplier": event.details["supplier"],
                                                  "purchase_order": purchase, "actual_arrival": event.timestamp,
                                                  "stock_before_delivery": event.state_before, "stock_after_delivery": event.state_after,
                                                  "snapshot_delivery": True})
    station_metrics = run.stations.set_index("ID_WS")
    features = {}
    for kind, key in entities:
        if kind == "station":
            times = episodes[key]
            waits = flow.stations[key]
            feature = {"episode_count": len(times), "working_downtime_minutes": sum(times),
                       "mean_episode_working_minutes": float(np.mean(times)) if times else None,
                       "max_episode_working_minutes": max(times, default=0.),
                       "rfq_assigned_operation_wait_working_minutes": operation_waits[key],
                       "rfq_capacity_wait_minutes": waits["rfq_capacity_wait_minutes"],
                       "rfq_failure_overlap_minutes": waits["rfq_failure_overlap_minutes"],
                       "factory_run_utilization": station_metrics.at[key, "utilization"]}
            traces["machine_exposures"].append({"trial": trial, "station": key, **feature})
        else:
            times = sampled_dlt[key]
            feature = {"mean_dlt_minutes": float(np.mean(times)) if times else None, "delivery_count": len(times),
                       "total_dlt_deviation_minutes": sum(value - Purchasing.nominal(materials[key]) for value in times),
                       "arrivals_while_rfq_short": arrivals_into_shortage[key],
                       "rfq_wait_for_material_working_minutes": sum(
                           minute_count(min(stop, row["until"]) - row["from"])
                           for row in requirements[key[0]] if row["short_wait_working_minutes"] and row["from"] < stop)}
            traces["supplier_exposures"].append({"trial": trial, "material": key[0], "supplier": key[1], **feature})
        features[kind, key] = feature
    return features


def analyze_operational_risk(sim, model, target, risk, settings, seeds, traces, *, sample_otp=False):
    """The simulator callback runs a fresh copy of the same RFQ state."""
    nominal = sim(seed=int(seeds[0]), scenario_id="risk_reference")
    deterministic = observation(nominal, target)
    order = model.orders[target]
    origin = order.RD
    machines = machine_reliability(model, risk)
    terms = supplier_lead_times(model)
    suppliers = [key for key, term in terms.items() if isinstance(term, Parameter) and term.sampler is not None]
    entities = [("station", key) for key in machines] + [("supply", key) for key in suppliers]
    reference = {"completion": deterministic[0] if pd.notna(deterministic[0]) else None,
                 "lead_minutes": minute_count(deterministic[0] - origin) if pd.notna(deterministic[0]) else None,
                 "status": nominal.experiment.iloc[0].status}
    variable_otp = sample_otp and any(isinstance(terms["OTP"], Parameter) and terms["OTP"].sampler is not None
                                      for terms in model.materials.values())
    if not entities and not variable_otp:
        return {"reference": reference, "modeled": False, "rankings": [], "interactions": []}

    risk = replace(risk, matched_supply_streams=True)
    features = defaultdict(list)
    full = []
    for trial, seed in enumerate(seeds, 1):
        flow = FlowObserver(target, trace_requirements=True, until=pd.Timestamp(risk.observation_end))
        run = sim(risk=risk, seed=int(seed), run_id=trial, scenario_id="full_risk", observer=flow)
        outcome = observation(run, target)
        full.append(outcome)
        stop = min(pd.Timestamp(risk.observation_end), outcome[1], outcome[0]) if pd.notna(outcome[0]) else min(
            pd.Timestamp(risk.observation_end), outcome[1])
        for key, row in record_exposures(run, flow, model, target, stop, terms, entities, trial, traces).items():
            features[key].append(row)
        traces["risk_outcomes"].append({"trial": trial, "seed": int(seed), "completion": outcome[0], "end": outcome[1],
                                        "status": run.experiment.iloc[0].status})

    full_samples = CompletionSamples(full, origin)
    lead = np.where(np.isfinite(full_samples.upper), full_samples.upper / NS_PER_MINUTE, np.nan)

    def control(entity_keys):
        config = replace(risk, controlled_stations=frozenset(key for kind, key in entity_keys if kind == "station"),
                         controlled_suppliers=frozenset(key for kind, key in entity_keys if kind == "supply"))
        return [observation(sim(risk=config, seed=int(seed), run_id=trial, scenario_id="risk_control"), target)
                for trial, seed in enumerate(seeds, 1)]

    rankings = []
    controlled = {}
    for kind, key in entities:
        entity = kind, key
        control_dates = control((entity,))
        samples = CompletionSamples(control_dates, origin)
        controlled[entity] = samples.upper
        effect = paired_effect(full_samples, samples, settings.practical_delay_minutes)
        table = pd.DataFrame(features[entity])
        metrics = ("working_downtime_minutes", "episode_count") if kind == "station" else ("mean_dlt_minutes", "total_dlt_deviation_minutes")
        label = "low observed sensitivity"
        exposed = True if kind == "station" else table.delivery_count.sum() > 0
        if (not exposed or effect["complete_pairs"] < settings.min_complete_pairs or
                effect["complete_pairs"] / effect["runs"] < settings.min_complete_fraction):
            label = "insufficient data"
        elif effect["material"]["lower"] >= settings.critical_probability and (effect["mean_complete_pairs_minutes"] or 0) > 0:
            label = "critical machine availability" if kind == "station" else "critical supplier"
        rankings.append({"type": kind, "entity": key, "label": label, **effect,
                         "exposures": {name: descriptive(table[name]) for name in table},
                         "correlations": {name: association(table[name], lead) for name in metrics}})
        for trial, (before, after) in enumerate(zip(full, control_dates, strict=True), 1):
            traces["risk_pairs"].append({"trial": trial, "type": kind, "entity": key, "full_completion": before[0],
                                         "control_completion": after[0], "full_end": before[1], "control_end": after[1],
                                         "delta_minutes": minute_count(before[0] - after[0]) if pd.notna(before[0]) and pd.notna(after[0]) else None})
    rankings.sort(key=lambda row: (row["label"] != "insufficient data", row["mean_complete_pairs_minutes"]
                                  if row["mean_complete_pairs_minutes"] is not None else -np.inf), reverse=True)
    interactions = []
    ranking_order = [(row["type"], row["entity"]) for row in rankings]
    for pair in islice(combinations(ranking_order, 2), settings.max_interaction_pairs):
        joint = CompletionSamples(control(pair), origin).upper
        first, second = (controlled[key] for key in pair)
        done = np.isfinite(full_samples.upper) & np.isfinite(first) & np.isfinite(second) & np.isfinite(joint)
        values = (full_samples.upper[done] - first[done] - second[done] + joint[done]) / NS_PER_MINUTE
        interval = mean_interval(values)
        interactions.append({"entities": list(pair), "complete_quartets": len(values), "runs": len(seeds),
                             "mean_minutes": float(values.mean()) if len(values) else None, "mc95": interval,
                             "nonzero_mc95": interval[0] is not None and (interval[0] > 0 or interval[1] < 0)})
        for trial, value in zip(np.flatnonzero(done) + 1, values, strict=True):
            traces["interaction_effects"].append({"trial": int(trial), "entities": pair, "interaction_minutes": float(value)})
    reference_samples = CompletionSamples([deterministic] * len(seeds), origin)
    remainder = None
    if controlled and pd.notna(deterministic[0]):
        valid = np.isfinite(full_samples.upper) & np.logical_and.reduce([np.isfinite(x) for x in controlled.values()])
        total = full_samples.upper[valid] - reference_samples.upper[valid]
        marginal = sum(full_samples.upper[valid] - column[valid] for column in controlled.values())
        values = (total - marginal) / NS_PER_MINUTE
        remainder = {"n": len(values), "mean_minutes": float(values.mean()) if len(values) else None, "mc95": mean_interval(values),
                     "includes_uncontrolled_order_cadence": variable_otp}
    return {"reference": reference, "modeled": True, "rankings": rankings, "interactions": interactions,
            "full_minus_nominal": paired_effect(full_samples, reference_samples, settings.practical_delay_minutes),
            "total_minus_sum_of_marginals": remainder,
            "observation_end": pd.Timestamp(risk.observation_end),
            "interaction_scope": "Ranked pairs up to the configured limit. Marginal effects can overlap."}
