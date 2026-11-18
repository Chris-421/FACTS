"""Association with realized disruptions over a fixed observation interval."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .model import minute_count


@dataclass
class SensitivityResult:
    observations: pd.DataFrame
    correlations: pd.DataFrame
    coefficients: pd.DataFrame
    diagnostics: pd.DataFrame


def _exposures(run, machine_exposure):
    features = {}
    cutoff = pd.Timestamp(run.configuration["risk"].observation_end)
    for row in run.sampled_inputs.itertuples(index=False):
        if row.parameter == "tbf_minutes":
            if machine_exposure in ("reference", "both"):
                key = "station", row.entity_id, "reference_failure_count"
                features[key] = features.get(key, 0) + int(row.details["exposure_eligible"])
            if machine_exposure in ("realized", "both"):
                features.setdefault(("station", row.entity_id, "realized_failure_count"), 0)
                features.setdefault(("station", row.entity_id, "realized_repair_minutes"), 0)
        elif row.parameter == "repair_minutes" and machine_exposure in ("reference", "both"):
            key = "station", row.entity_id, "reference_repair_minutes"
            features[key] = features.get(key, 0) + (row.sampled_value if row.details["exposure_eligible"] else 0)
        elif row.parameter == "lead_time_minutes" and row.details["exposure_eligible"]:
            key = "supplier", row.entity_id, "total_lead_time_deviation_minutes"
            features[key] = features.get(key, 0) + row.details["delay_minutes"]
    if machine_exposure in ("realized", "both"):
        outages = run.event_log.loc[(run.event_log.event_type == "unavailability_start") &
                                    (run.event_log.timestamp < cutoff)]
        for event in outages.itertuples(index=False):
            if event.details["reason"] != "MACHINE_FAILURE":
                continue
            end = min(event.details["t_done"], cutoff)
            features["station", event.entity_id, "realized_failure_count"] += 1
            features["station", event.entity_id, "realized_repair_minutes"] += minute_count(end - event.timestamp)
    return features


def analyze_risk(experiment, order_id, *, scenario_id="rfq", predictors=None,
                 machine_exposure="reference", outcome="completion_minutes"):
    """Relate order outcomes to disruption draws or fixed-window realized outages."""
    if machine_exposure not in ("reference", "realized", "both"):
        raise ValueError("machine_exposure must be 'reference', 'realized' or 'both'")
    outcomes = {"completion_minutes", "tardiness_minutes", "waiting_minutes"}
    if outcome not in outcomes:
        raise ValueError(f"Outcome must be one of {sorted(outcomes)}")
    rows = []
    inputs = set()
    incomplete_exposure = 0
    for run in experiment.runs:
        if run.configuration["risk"] is None:
            raise ValueError("Sensitivity requires an operational-risk Monte Carlo experiment")
        metadata = run.experiment.iloc[0]
        if metadata["scenario_id"] != scenario_id:
            continue
        order = run.orders.loc[run.orders["ID_O"].map(lambda value: value == order_id).astype(bool)]
        if len(order) != 1:
            raise ValueError(f"Order {order_id!r} is missing from scenario {scenario_id!r}")
        order = order.iloc[0]
        start = run.initial["F"].attrs["snapshot_time"]
        value = np.nan
        exposure_cutoff = pd.Timestamp(run.configuration["risk"].observation_end)
        truncated = (machine_exposure != "reference" and metadata["status"] == "limit_reached"
                     and metadata["end_time"] < exposure_cutoff)
        incomplete_exposure += int(order["completed"] and truncated)
        if order["completed"] and not truncated:
            if outcome == "completion_minutes":
                value = minute_count(order["completion_time"] - start)
            elif outcome == "tardiness_minutes":
                value = minute_count(order["tardiness"])
            else:
                value = order["waiting_minutes"]
        features = _exposures(run, machine_exposure)
        inputs.update(features)
        rows.append((metadata["run_id"], value, features))
    if not rows:
        raise ValueError(f"No runs for scenario {scenario_id!r}")
    features = sorted(inputs, key=repr)
    values = pd.Series([row[1] for row in rows], name=outcome, dtype=float)
    realized = pd.DataFrame([[row[2].get(key, 0) for key in features] for row in rows], columns=features)
    observed = realized.loc[values.notna()]
    y = values.dropna()
    correlations = []
    for key in features:
        series = observed[key]
        varying = len(y) >= 2 and y.nunique() > 1 and series.nunique() > 1
        pearson = series.corr(y) if varying else np.nan
        spearman = series.rank().corr(y.rank()) if varying else np.nan
        correlations.append((*key, len(y), pearson, spearman))
    included = [key for key in features if observed[key].nunique() > 1]
    if predictors is not None:
        unknown = set(predictors) - inputs
        if unknown:
            raise ValueError(f"Unknown sensitivity predictors: {unknown}")
        included = [key for key in included if key in predictors]
    coefficient_rows = []
    rank, residual_df, r2 = None, None, np.nan
    status = "insufficient_variation"
    if included and len(y) > 1 and y.nunique() > 1:
        design = np.column_stack([np.ones(len(y)), observed[included].to_numpy(dtype=float)])
        coefficients, _, rank, _ = np.linalg.lstsq(design, y.to_numpy(), rcond=None)
        residual_df = len(y) - rank
        status = "rank_deficient" if rank < design.shape[1] else "insufficient_residual_df"
        if rank == design.shape[1] and residual_df > 0:
            residual = y.to_numpy() - design @ coefficients
            sse = float(residual @ residual)
            centered = y.to_numpy() - y.mean()
            r2 = 1 - sse / float(centered @ centered)
            standard_errors = np.sqrt(np.diag(np.linalg.pinv(design.T @ design)) * sse / residual_df)
            coefficient_rows = [(*key, coefficient, error) for key, coefficient, error in
                                zip([("model", "intercept", "intercept"), *included], coefficients, standard_errors)]
            status = "fit"
    observations = pd.concat([pd.DataFrame({"run_id": [row[0] for row in rows], outcome: values}), realized], axis=1)
    key_cols = ["entity_type", "entity_id", "metric"]
    return SensitivityResult(observations,
                             pd.DataFrame(correlations, columns=[*key_cols, "n", "pearson", "spearman"]),
                             pd.DataFrame(coefficient_rows, columns=[*key_cols, "coefficient", "standard_error"]),
                             pd.DataFrame([{"status": status, "n": len(y),
                                            "excluded_unfinished": len(rows) - len(y) - incomplete_exposure,
                                            "excluded_incomplete_exposure": incomplete_exposure,
                                            "rank": rank, "residual_df": residual_df, "r_squared": r2}]))
