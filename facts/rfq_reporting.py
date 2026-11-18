"""Planner-facing Markdown and machine-readable RFQ results."""

from datetime import date, time
from decimal import Decimal
import gzip
import json
from math import isfinite
from pathlib import Path

import numpy as np
import pandas as pd


def _serial(value):
    if value is pd.NaT or value is pd.NA:
        return None
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return _serial(value.item())
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return value if isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _serial(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serial(item) for item in value]
    return value


def _date(value):
    return "Unidentified" if value is None else pd.Timestamp(value).isoformat(sep=" ", timespec="seconds")


def _number(value, *, signed=False):
    if value is None:
        return "—"
    return f"{value:+,.1f}" if signed else f"{value:,.1f}"


def _probability(result):
    if result["lower"] == result["upper"]:
        return f'{result["lower"]:.1%}'
    return f'{result["lower"]:.1%}–{result["upper"]:.1%}'


def _correlation(value):
    return "—" if value is None else f"{value:+.2f}"


def _date_span(result):
    return f'{_date(result["lower"])} to {_date(result["upper"])}' if result is not None else "Unidentified"


def _label(value):
    if isinstance(value, tuple):
        value = " / ".join(str(item) for item in value)
    return str(value).replace("|", r"\|").replace("\n", " ")


def render_rfq_markdown(decision):
    """Render the five decision sections from the same structured result."""
    result = decision.report
    commitment = result["commitment"]
    forecasts = result["pareto_sensitivity"]
    limits = result["settings"]
    planner = result["planner"]
    quantiles = ("p50", "p80", "p90", "p95")
    forecast = commitment["forecast"]
    title = _label(result["rfq_id"])
    report = [f"# FACTS RFQ decision · {title}", "", "## A. Recommended commitment", ""]
    if commitment["recommended_commitment"] is None:
        report.append("**Commitment unavailable.** The selected production quantile or a feasible dispatch time could not be identified.")
    else:
        report.append(f'**Recommended {commitment["commitment_basis"]} · {_date(commitment["recommended_commitment"])}**')
    report.extend(["", f'The {commitment["threshold"]:.0%} policy uses the planner-weighted, capped 80/20 production forecast. '
                   f'The production quantile is {_date(commitment["recommended_production_completion"])}. '
                   f'The requested date is {_date(commitment["customer_requested_date"])}. '
                   f'Modeled production on-time probability is {_probability(forecast["on_time"])}. '
                   f'{forecast["completed"]:,} of {forecast["runs"]:,} trajectories completed by their observation stop.',
                   "", "Transportation is outside the model. Quantile intervals describe Monte Carlo sampling uncertainty."])
    q_interval = commitment["production_quantile"]["mc_interval"]
    report.extend(["", f'Commitment quantile 95% Monte Carlo interval **{_date(q_interval[0])} to {_date(q_interval[1])}**. '
                   f'Mean production completion {_date(forecast["mean_completion"])}. '
                   f'Completion standard deviation {_number(forecast["std_minutes"])} minutes.'])
    if "dispatch_on_time" in commitment:
        report.append(f'With the configured cutoff, modeled dispatch on-time probability is {_probability(commitment["dispatch_on_time"])}.')
    if "estimate" in planner:
        report.extend(["", f'Planner estimate {planner["estimate"]:.0%} with {planner["certainty"]} certainty and '
                       f'{planner["minimum"]:.0%}–{planner["maximum"]:.0%} plausible bounds. '
                       f'The probability curve uses a {planner["half_weight_pp"][planner["certainty"].lower()]:g}-point half-weight distance.'])
    else:
        report.extend(["", "Planner supplied the efficiency probability weights directly."])
    report.extend(["", "## B. Generic efficiency sensitivity", "",
                   "| Underlying Pareto | P50 | P80 | P90 | P95 | Mean completion | SD (min) | On time |",
                   "|---|---|---|---|---|---|---:|---:|"])
    for name in ("70/30", "80/20", "90/10"):
        scenario = forecasts["assumptions"][name]["forecast"]
        dates = " | ".join(_date(scenario["quantiles"][q]["date"]) for q in quantiles)
        report.append(f'| {name} | {dates} | {_date(scenario["mean_completion"])} | '
                      f'{_number(scenario["std_minutes"])} | {_probability(scenario["on_time"])} |')
    ranges = forecasts["ranges"]
    report.extend(["", f'P90 assumption range **{_date_span(ranges["p90"])}**. P95 range **{_date_span(ranges["p95"])}**. '
                   f'On-time probability range **{_probability(ranges["on_time"])}**. '
                   "These ranges reflect the tested assumptions and carry no forecast coverage level. "
                   "The JSON includes separate sampling intervals for each percentile and shape."])

    capacity = result["capacity"]
    factory = capacity["main_factory_constraint"]
    rfq = capacity["main_rfq_constraint"]
    report.extend(["", "## C. Production constraints", "",
                   f'Main factory queue constraint **{_label(factory) if factory is not None else "none observed"}**. '
                   f'Main RFQ capacity wait **{_label(rfq) if rfq is not None else "none observed"}**.',
                   "", "| Group | Utilization | Mean queue | Mean RFQ queue (working min) | RFQ capacity wait (working min) |",
                   "|---|---:|---:|---:|---:|"])
    for row in capacity["resources"]:
        if row["resource_type"] == "group":
            util = row["processing_utilization"]
            report.append(f'| {_label(row["resource"])} | {f"{util:.1%}" if util is not None else "—"} | '
                          f'{_number(row["mean_queue_length"])} | {_number(row["mean_rfq_queue_minutes"])} | '
                          f'{_number(row["mean_rfq_capacity_wait_minutes"])} |')

    risk = result["operational_risk"]
    effects = risk["rankings"]
    report.extend(["", "## D. Critical operational risks", "",
                   f'Nominal reference RFQ lead time **{_number(risk["reference"]["lead_minutes"])} minutes** from release. '
                   "This section measures explicit disruptions with generic efficiency fixed at 100% and has no role in the quote."])
    if effects:
        critical = [_label(row["entity"]) for row in effects if row["label"].startswith("critical")]
        report.extend(["", f'Critical entities **{", ".join(critical) if critical else "none meeting the configured thresholds"}**.',
                       "", "| Entity | Assessment | Complete-pair mean LT effect (min) | P90 paired effect (min) | Material-delay probability | P90 exposure (min) | Pearson/Spearman |",
                       "|---|---|---:|---:|---:|---:|---|"])
        for row in effects:
            metric = "working_downtime_minutes" if row["type"] == "station" else "mean_dlt_minutes"
            correlation = row["correlations"][metric]
            report.append(f'| {_label(row["entity"])} | {row["label"]} | {_number(row["mean_complete_pairs_minutes"], signed=True)} | '
                          f'{_number(row["p90_minutes"]["estimate"], signed=True)} | {_probability(row["material"])} | '
                          f'{_number(row["exposures"][metric]["p90"])} | {_correlation(correlation["pearson"])} / '
                          f'{_correlation(correlation["spearman"])} (n={correlation["n"]}) |')
        report.extend(["", "The ranking uses paired entity controls. Correlations describe exposure and lead time among observed outcomes. "
                       "Marginal effects can overlap."])
        for row in risk["interactions"]:
            if row["nonzero_mc95"]:
                names = " and ".join(_label(entity) for _, entity in row["entities"])
                report.extend(["", f'{names} showed a mean interaction of {_number(row["mean_minutes"], signed=True)} minutes '
                                  f'across {row["complete_quartets"]:,} complete paired sets.'])
    elif not risk["modeled"]:
        report.extend(["", "The supplied configuration has no explicit machine or supplier risks to rank."])
    else:
        report.extend(["", "Stochastic ordering cadence is modeled. The configuration has no machine failure or supplier DLT risks to rank."])

    impact = result["existing_order_impact"]
    report.extend(["", "## E. Existing commitments", "",
                   f'**{impact["materially_delayed_orders"]}** existing orders met the flag threshold of a '
                   f'{limits["critical_probability"]:.0%} chance of delay exceeding {limits["practical_delay_minutes"]:g} minutes.'])
    if impact["orders"]:
        report.extend(["", "| Order | Mean paired change (min) | Change in scenario P90 (min) | P90 of paired changes (min) | Miss-probability change (pp) |",
                       "|---|---:|---:|---:|---:|"])
        for row in impact["orders"]:
            change = row["miss_probability_change"]["estimate"]
            report.append(f'| {_label(row["order"])} | {_number(row["mean_minutes"], signed=True)} | '
                          f'{_number(row["marginal_p90_change_minutes"], signed=True)} | {_number(row["p90_minutes"]["estimate"], signed=True)} | '
                          f'{_number(100 * change if change is not None else None, signed=True)} |')
    report.extend(["", f'Seed {limits["seed"]}. Observation cutoff {_date(limits["until"])}. '
                   "The quote is conditional on the model and has yet to be validated against real HMLV completions."])
    return "\n".join(report) + "\n"


def write_rfq_report(decision, directory, *, include_traces=True):
    """Write JSON and Markdown with separate, optionally archived trace tables."""
    output = Path(directory)
    output.mkdir(parents=True, exist_ok=True)
    (output / "decision.json").write_text(json.dumps(_serial(decision.report), ensure_ascii=False, allow_nan=False, indent=2)
                                          + "\n", encoding="utf-8")
    (output / "decision.md").write_text(render_rfq_markdown(decision), encoding="utf-8")
    if include_traces:
        traces = output / "traces"
        traces.mkdir(exist_ok=True)
        for name, table in decision.traces.items():
            with gzip.open(traces / f"{name}.jsonl.gz", "wt", encoding="utf-8", compresslevel=2) as stream:
                for row in table.to_dict("records"):
                    stream.write(json.dumps(_serial(row), ensure_ascii=False, allow_nan=False) + "\n")
    return output
