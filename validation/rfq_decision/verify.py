"""Check historical artefacts and replay the archived deterministic schedules."""

from dataclasses import asdict
import gzip
from hashlib import file_digest, sha256
import json
from pathlib import Path
import subprocess

import numpy as np
import pandas as pd

from facts import ParetoDowntime, PoissonEfficiencySettings, RunLimits, simulate
from validation.adapters.job_shop import Case
from validation.efficiency_intensity.fixtures import saturated, shift_close
from validation.efficiency_v2.model import PARETO_SCENARIOS, simulate_v2
from validation.experiments.benchmark import _evaluate_facts


ROOT = Path(__file__).resolve().parent
BASE = ROOT.parents[1]
VALIDATION = ROOT.parent


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def preserve():
    manifest = json.loads((ROOT / "preserved_baseline.json").read_text(encoding="utf-8"))
    for name, value in manifest["fixed_files"].items():
        assert digest(BASE / name) == value, name
    for name, prefix in manifest["report_prefixes"].items():
        with (BASE / name).open("rb") as stream:
            assert sha256(stream.read(prefix["bytes"])).hexdigest() == prefix["sha256"], name
    return len(manifest["fixed_files"]), len(manifest["report_prefixes"])


def benchmark():
    adapted = VALIDATION / "data/adapted/benchmark/cases.json"
    cases = [Case(**case) for case in json.loads(adapted.read_text(encoding="utf-8"))]
    with gzip.open(VALIDATION / "results/raw_outputs/benchmark/facts_journals.jsonl.gz", "rt", encoding="utf-8") as stream:
        archived = {row["case_id"]: row for row in map(json.loads, stream)}
    count = 0
    for case in cases:
        run, placements, checked, _ = _evaluate_facts(case)
        assert run.experiment.status.item() == "complete" and checked.feasible
        assert [asdict(row) for row in placements] == archived[case.id]["operation_placements"], case.id
        count += len(placements)
    return len(cases), count


def poisson_replay():
    summary = []
    for duration, shifts, eta, seed, idle in ((18, 3, .9, 147, "immediate"), (45, 10, .6, 375, "immediate"),
                                               (90, 20, .75, 849, "immediate"), (72, 5, .75, 731, "next_assignment")):
        data, state = saturated(duration, shifts)
        limits = RunLimits(shift_close(shifts))
        old = simulate_v2(data, state, efficiency=eta, duration=PARETO_SCENARIOS["v2_80_cap480"],
                          idle_policy=idle, seed=seed, limits=limits)
        current = simulate(data, state, efficiency=PoissonEfficiencySettings(eta, ParetoDowntime(), idle),
                           seed=seed, limits=limits)
        old_dates = old.orders.completion_time
        current_dates = current.orders.completion_time
        pd.testing.assert_series_equal(old_dates.notna(), current_dates.notna())
        occupied = old_dates.notna()
        delta = (old_dates[occupied] - current_dates[occupied]).abs()
        worst_difference = int(delta.max().value) if len(delta) else 0
        assert worst_difference <= 100, worst_difference
        for col in ("generic_unavailability_time", "generic_downtime_generated", "generic_downtime_outstanding",
                    "generic_downtime_pending", "observed_shift_time", "processing_time"):
            np.testing.assert_allclose(old.stations[col], current.stations[col], rtol=1e-12, atol=2e-6)
        summary.append({"operation_minutes": duration, "shifts": shifts, "eta": eta, "seed": seed, "activation": idle,
                        "maximum_completion_difference_ns": worst_difference})
    return summary


def main():
    files, docs = preserve()
    cases, placements = benchmark()
    replay = poisson_replay()
    result = {"implementation_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=BASE, text=True).strip(),
              "historical_files_verified": files, "document_prefixes_verified": docs,
              "deterministic_schedules_replayed": cases, "identical_placements": placements, "frozen_v2_replays": replay}
    (ROOT / "verification.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Preserved {files} files and matched {placements} placements across {cases} schedules")


if __name__ == "__main__":
    main()
