"""Run the complete decision workflow on a synthetic job shop."""

import argparse
from hashlib import file_digest
import json
from pathlib import Path
import subprocess
from time import perf_counter

import pandas as pd

from examples.synthetic import frame, weekly
from facts import (FactoryData, MachineReliability, Parameter, PlannerEfficiency, RFQSettings, RiskSettings,
                   Snapshot, assess_rfq, write_rfq_report)


START = pd.Timestamp("2026-11-16 08:00")
HORIZON = pd.Timestamp("2026-11-24 16:00")
RELEASE = START + pd.Timedelta(minutes=20)


def decision_factory():
    data = FactoryData(
        frame("FACTS", [("rfq-demo", "Decision fixture", "job shop", 3, weekly(), {}, "EDD", {})]),
        frame("Parts", [("existing", ["mill-old", "finish-old"], {}), ("panel", ["mill-panel", "finish-panel"], {1: {"alloy": 1}}),
                        ("check", ["finish-check"], {}), ("side", ["punch"], {})]),
        frame("Processes", [("mill-old", 50, 0, ["milling"]), ("finish-old", 60, 0, ["finishing"]),
                            ("mill-panel", 65, 0, ["milling"]), ("finish-panel", 95, 10, ["finishing"]),
                            ("finish-check", 40, 0, ["finishing"]), ("punch", 20, 0, ["punching"])]),
        frame("Workstations", [("Mill A", "milling", True, None), ("Booth B", "finishing", True, None),
                               ("Punch C", "punching", True, None)]),
        frame("Orders", [("E-01", [("existing", 1)], START, START + pd.Timedelta(hours=8), "outstanding"),
                         ("E-02", [("existing", 1)], START + pd.Timedelta(minutes=25), START + pd.Timedelta(hours=7.5), "outstanding"),
                         ("E-03", [("existing", 1)], START + pd.Timedelta(minutes=70), START + pd.Timedelta(days=1, hours=4), "outstanding"),
                         ("SIDE", [("side", 1)] * 6, START, START + pd.Timedelta(days=3), "outstanding")]),
        frame("Materials", [("alloy", "stock", ["Metals Co"], 5, Parameter(95, lambda rng: rng.uniform(100, 415)), 2880, None)]),
    )
    inventory = pd.DataFrame([{"ID_mat": "alloy", "cq": 0, "next_order_time": START + pd.Timedelta(minutes=10)}])
    initial = Snapshot(START, inventory=inventory)
    rfq = frame("Orders", [("RFQ-21", [("panel", 1), ("check", 1)], RELEASE, START + pd.Timedelta(hours=7), "outstanding")])
    risk = RiskSettings(pd.Timestamp("2026-11-18 16:00"), {
        "Booth B": MachineReliability(lambda rng: rng.uniform(110, 220), lambda rng: rng.uniform(75, 180), 35),
        "Punch C": MachineReliability(30, 20, 15),
    })
    return data, initial, rfq, risk


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=2000)
    parser.add_argument("--risk-runs", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20261120)
    parser.add_argument("--output", type=Path, default=Path("validation/rfq_decision/example"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=False)
        git_commit = revision.stdout.strip() or None
    except FileNotFoundError:
        git_commit = None
    source_files = [*sorted((root / "facts").glob("*.py")), Path(__file__), root / "examples/synthetic.py"]
    hashes = {}
    for source in source_files:
        with source.open("rb") as stream:
            hashes[source.resolve().relative_to(root).as_posix()] = file_digest(stream, "sha256").hexdigest()
    data, initial, rfq, risk = decision_factory()
    planner = PlannerEfficiency(.75, "medium", .65, .85)
    settings = RFQSettings(HORIZON, runs=args.runs, risk_runs=args.risk_runs, seed=args.seed,
                           dispatch_cutoff="14:30", practical_delay_minutes=45.)
    began = perf_counter()
    decision = assess_rfq(data, initial, rfq, planner=planner, settings=settings, risk=risk)
    simulated_seconds = perf_counter() - began
    write_rfq_report(decision, args.output)
    metadata = {"scenario": "multi-job RFQ on a three-station job shop", "seed": args.seed, "forecast_paths_per_law": args.runs,
                "operational_paths_per_control": args.risk_runs, "simulation_seconds": simulated_seconds,
                "total_seconds": perf_counter() - began, "settings": decision.report["settings"],
                "risk_observation_end": str(risk.observation_end), "implementation_commit": git_commit,
                "source_sha256": hashes}
    (args.output / "run.json").write_text(json.dumps(metadata, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"Wrote {args.output} in {metadata['total_seconds']:.2f} seconds", flush=True)


if __name__ == "__main__":
    main()
