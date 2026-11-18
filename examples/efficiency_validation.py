"""Exercise the proposed efficiency equation on one shift with saturated demand."""

import argparse
from dataclasses import dataclass

import pandas as pd

from examples.synthetic import frame, weekly
from facts import EfficiencySettings, FactoryData, RunLimits, Snapshot, monte_carlo


@dataclass(frozen=True)
class Case:
    name: str
    minutes_per_unit: int = 18
    quantity: int = 1
    stations: int = 1
    policy: str = "EDD"
    shape: float = 1.161


CASES = (Case("reference"), Case("larger lot", quantity=4), Case("long operation", minutes_per_unit=90),
         Case("two stations", stations=2), Case("FIFO", policy="FIFO"), Case("lighter tail", shape=2))


def validate_efficiency(*, runs, seed, cases=CASES, levels=(0.75, 0.9, 1.0)):
    start = pd.Timestamp("2026-11-16 08:00")
    end = start + pd.Timedelta(hours=8)
    capacity_rows = []
    delivery_rows = []
    for case in cases:
        operation = case.minutes_per_unit * case.quantity
        backlog = case.stations * (480 // operation + 2)
        # The target leads the queue; the remaining lots keep all eligible stations supplied.
        lines = [("target", [("part", case.quantity)], start, end, "outstanding")]
        lines.extend((f"backlog-{i}", [("part", case.quantity)], start, end + pd.Timedelta(days=7), "outstanding")
                     for i in range(backlog))
        data = FactoryData(
            frame("FACTS", [("F", "Efficiency validation", "synthetic", 1, weekly(), {}, case.policy, {})]),
            frame("Parts", [("part", ["op"], {})]),
            frame("Processes", [("op", case.minutes_per_unit, 0, ["work"])]),
            frame("Workstations", [(f"S{i}", "work", True, None) for i in range(case.stations)]),
            frame("Orders", lines), frame("Materials"),
        )
        mc = monte_carlo(data, Snapshot(start), runs=runs, seed=seed, efficiency_levels=levels,
                         efficiency_settings=EfficiencySettings(1, pareto_shape=case.shape), limits=RunLimits(end))
        shifts = pd.concat([run.station_shifts for run in mc.runs], ignore_index=True)
        shifts = shifts.loc[shifts.shift_start == start]
        for level, group in shifts.groupby("efficiency_assumed", sort=False):
            elapsed = group.observed_shift_time.sum()
            achieved_loss = group.generic_unavailability_time.sum() / elapsed
            capacity_rows.append({"case": case.name, "efficiency": level, "operation_minutes": operation,
                                  "station_shifts": len(group), "target_loss": 1 - level,
                                  "realized_loss": achieved_loss, "loss_bias": achieved_loss - (1 - level),
                                  "unused_budget_min": group.loss_budget_remaining.mean(),
                                  "available_capacity": 1 - achieved_loss,
                                  "processing_utilization": group.processing_time.sum() / elapsed})
        delivery_rows.append(mc.conditional_summary.loc[lambda rows: rows.ID_O == "target"].assign(case=case.name))
    return pd.DataFrame(capacity_rows), pd.concat(delivery_rows, ignore_index=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=30, help="replications per efficiency and case")
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    capacity, delivery = validate_efficiency(runs=args.runs, seed=args.seed)
    print(capacity.round(4).to_string(index=False))
    print("\nTarget-order completion distribution")
    print(delivery[["case", "efficiency_level", "completion_probability", "median", "p90", "p95"]].to_string(index=False))


if __name__ == "__main__":
    main()
