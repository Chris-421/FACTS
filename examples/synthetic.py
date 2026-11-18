"""Small factory with WIP, step-level materials, and a prospective RFQ."""

from dataclasses import replace

import pandas as pd

from facts import (FactoryData, MachineReliability, Parameter, RiskSettings, Snapshot, analyze_risk,
                   compare_rfq, monte_carlo)
from facts.model import TABLES


def frame(name, rows=()):
    return pd.DataFrame(rows, columns=TABLES[name])


def weekly(start="08:00", end="16:00"):
    return [{"shift_number": day, "Shift_ID": "day", "weekday": day,
             "start": start, "end": end} for day in range(5)]


def example_factory():
    factory = frame("FACTS", [("demo", "Example Works", "fabrication", 3, weekly(), {}, "EDD", {})])
    parts = frame("Parts", [("bracket", ["cut", "finish"], {1: {"paint": 0.5}})])
    processes = frame("Processes", [("cut", {"bracket": 30}, None, ["saw"]),
                                    ("finish", {"bracket": 45}, 15, ["booth"])])
    stations = frame("Workstations", [("Saw A", "saw", True, None), ("Booth B", "booth", True, None)])
    stations["previous_part"] = [None, "bracket"]
    orders = frame("Orders", [("existing", [("bracket", 4)], "2026-11-13 09:00", "2026-11-16 13:00", "outstanding")])
    materials = frame("Materials", [("paint", "coatings", ["Paint Co"], 5, 90, 180, "2026-11-13 14:00")])
    wip = pd.DataFrame([{"ID_O": "existing", "line": 1, "state": "processing", "step": 0,
                         "ID_WS": "Saw A", "remaining_minutes": 45}])
    stock = pd.DataFrame([{"ID_mat": "paint", "cq": 1}])
    deliveries = pd.DataFrame([{"ID_PO": "PO-1", "ID_mat": "paint", "q": 2, "arrival": "2026-11-16 10:00"},
                               {"ID_PO": "PO-2", "ID_mat": "paint", "q": 5, "arrival": "2026-11-17 07:00"}])
    initial = Snapshot("2026-11-16 08:00", wip, stock, deliveries)
    rfq = frame("Orders", [("rfq-01", [("bracket", 2)], "2026-11-16 08:30", "2026-11-16 12:00", "outstanding")])
    return FactoryData(factory, parts, processes, stations, orders, materials), initial, rfq


def main():
    data, initial, rfq = example_factory()
    comparison = compare_rfq(data, initial, rfq)
    print("Deterministic RFQ scenario")
    print(comparison.accepted.orders[["ID_O", "completion_time", "tardiness"]].to_string(index=False))
    print("\nExisting order impact")
    print(comparison.existing_orders[["ID_O", "completion_time_change", "waiting_minutes_change"]].to_string(index=False))
    mc = monte_carlo(data, initial, rfq=rfq, runs=20, seed=2026, planner_weights={0.80: 0.25, 0.90: 0.75})
    print("\nDelivery forecast with illustrative planner-supplied weights")
    print(mc.weighted_summary[["ID_O", "median", "p90", "conditional_on_time_probability"]].to_string(index=False))

    materials = data.Materials.copy()
    materials["DLT"] = [Parameter(90, lambda rng: rng.uniform(45, 180))]
    risk_data = replace(data, Materials=materials)
    risk_stock = initial.inventory.assign(next_order_time="2026-11-16 08:15")
    risk_initial = replace(initial, inventory=risk_stock, pending_deliveries=None)
    risk = RiskSettings("2026-11-16 16:00", {"Booth B": MachineReliability(
        tbf=lambda rng: rng.uniform(120, 420), ttr=lambda rng: rng.uniform(10, 90), initial_tbf_remaining=80)})
    experiment = monte_carlo(risk_data, risk_initial, rfq=rfq, runs=40, seed=2026, risk=risk, compare=True)
    analysis = analyze_risk(experiment, "rfq-01")
    print("\nRFQ risk associations in a fixed exposure window")
    print(analysis.correlations.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
