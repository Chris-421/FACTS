# FACTS V0

FACTS estimates delivery dates for high-mix, low-volume factories. The same discrete-event engine runs the deterministic baseline, generic efficiency Monte Carlo, and explicit operational-risk Monte Carlo. An RFQ can be added to a copy of the starting scenario to measure its effects on existing orders.

## Run the examples and tests

The local environment uses Python 3.13. To recreate it, run `python -m venv .venv` and `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`.

```powershell
.\.venv\Scripts\python.exe -m examples.synthetic
.\.venv\Scripts\python.exe -m examples.efficiency_validation --runs 30 --seed 2026
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

`examples.synthetic` exercises all three modes. Its deterministic RFQ completes on 16 November 2026 at 11:30 and delays the existing order by 90 minutes.

## Input tables

Pass six pandas DataFrames to `FactoryData`, or use `load_workbook(path, start=...)` with six Excel sheets of the same names. All durations are in minutes and timestamps use factory-local time without a timezone. IDs uniquely identify rows within their own table. In Excel, enter nested lists and mappings as Python literals, such as `['cut', 'finish']` or `{1: {'paint': 0.5}}`.

| DataFrame | Required columns | Nested fields and optional columns |
|---|---|---|
| `FACTS` | `factory_ID`, `factory_name`, `industry`, `n_employees`, `shift_schedule`, `shift_exceptions`, `scheduling_policy`, `priority_rules` | One row. `shift_schedule` contains records with `shift_number`, `Shift_ID`, `weekday` (Monday = 0), `start`, and `end`. `shift_exceptions` maps a date to replacement shift records for that date, or `[]` for closure. `scheduling_policy` is `FIFO` or `EDD`. `{}` uses the confirmed priorities. |
| `Parts` | `ID_pt`, `routing`, `BOM` | `routing` is a list of process IDs. `BOM` maps zero-based routing steps to material quantities per part unit. Omitted steps use no extra material. A list with one material mapping per step is also accepted. |
| `Processes` | `ID_proc`, `tp`, `tsu`, `WS_groups` | `tp` is minutes per unit or a map from part ID to minutes per unit. `tsu` is blank, a fixed number, or a map from `(previous_part, current_part)` to minutes. `WS_groups` lists compatible groups. |
| `Workstations` | `ID_WS`, `WS_group`, `availability`, `t_down` | `availability` is `available`, `processing`, or `unavailable`. Boolean values map to `available` and `unavailable`. For initially unavailable stations, `t_down` is the remaining time before recovery. Optional columns include `previous_part` and `unavailability_reason`. |
| `Orders` | `ID_O`, `lines`, `RD`, `DD`, `status` | `lines` is a list of `(part_id, quantity)` pairs. Each line is a job, and the last job to finish determines the order completion time. `status` is `completed` or `outstanding`. RFQs use the same schema with distinct IDs. |
| `Materials` | `ID_mat`, `family`, `suppliers`, `q`, `DLT`, `OTP`, `last_order` | `suppliers` is a list. `q` is purchase quantity, `DLT` is lead time, and `OTP` is time from one placement to the next. `DLT` can map supplier IDs to their lead times. A material with several suppliers needs an explicit selector when it places an order. |

Regular shift numbers start at zero and repeat weekly. An end time earlier than the start time carries into the next day. Date exceptions replace occurrences that start on that date. The loader rejects overlapping occurrences, including overlaps across midnight. A job can start only if the full setup and processing time fits within the active shift.

### Starting snapshot

Construct `Snapshot(start, wip=..., inventory=..., pending_deliveries=...)` or use the optional Excel sheets below.

| Sheet | Fields |
|---|---|
| `WIP` | `ID_O`, one-based `line`, `state`, zero-based `step`. Supply `ID_WS` and `remaining_minutes` for processing jobs, `ready_at` for the current queue arrival of waiting jobs, and `completed_at` for finished lines in outstanding orders. An outstanding order released before `start` needs one WIP row per line. |
| `Inventory` | `ID_mat`, `cq` for each material, and `next_order_time` when another purchase is scheduled. `InventoryRules.initialize_order` can supply an explicit first-order policy using the material record and snapshot timestamp. |
| `PendingDeliveries` | `ID_PO`, `ID_mat`, `q`, `arrival`, and optional `supplier`. These carry their expected arrival into each scenario. |

Stock is consumed at the start of the step that needs it. Shortages make that step ineligible until sufficient stock arrives. The quantity and arrival of a new purchase are determined at placement, and stock changes at delivery. A blank `next_order_time` with no initialization policy means there is no scheduled next placement.

FIFO ranks by arrival at the current process queue. EDD ranks by order due date. Ties use the shortest eligible operation including setup, then queue arrival, then the input order of jobs. Eligible stations rank by shortest total operation duration, with workstation input order deciding a tie. A lower-priority job can run when a higher-priority job does not fit. `priority_rules` accepts explicitly configured `fifo_basis`, `backfill`, `job_tiebreak`, or `station_selection` overrides. The last two accept `'id'` or a rank function in Python.

## Python API

```python
from examples.synthetic import example_factory
from facts import compare_rfq, monte_carlo, simulate

data, initial, rfq = example_factory()
baseline = simulate(data, initial)
comparison = compare_rfq(data, initial, rfq)
forecast = monte_carlo(
    data, initial, rfq=rfq, compare=True, seed=2026,
    runs={0.80: 20, 0.90: 50}, planner_weights={0.80: 0.25, 0.90: 0.75},
)
print(comparison.existing_orders)
print(forecast.weighted_summary)
```

`simulate` returns initial and final snapshots (`F`, `J`, `S`, `Inv`), job and order results, station and station-shift metrics, an event log, sampled inputs, and run diagnostics. `compare_rfq` provides differences in completion, tardiness, waiting time, and workstation occupancy. Results distinguish `complete`, `blocked`, `infeasible`, and `limit_reached`. `RunLimits` accepts a timestamp cutoff and an event limit, which defaults to 20,000. Waiting time measures the portion observed from the snapshot onward.

### Efficiency forecast

Supply either a point estimate as `efficiency_levels=0.90`, a grid of conditional scenarios, or planner weights as a mapping or an `Efficiency_Beliefs` DataFrame with `efficiency_level` and `probability_weight`. A weighted forecast combines sample mass across levels before computing dates and percentiles. Incomplete runs retain their probability mass. Where completion or on-time performance is undetermined, the summary reports identifiable quantiles and on-time bounds. The spread is P95 minus P05.

`EfficiencySettings` configures the Pareto minimum, shape, and optional sampler. The capped loss can be shorter than the minimum. For a run that begins partway through a shift, supply the remaining loss budget by station with `initial_remaining` or provide a callable. Realized downtime, unused budget, idle time, and processing utilization are reported separately.

### Operational risk

Pass `risk=RiskSettings(observation_end, reliability)` to `simulate` or `monte_carlo`. For each modeled machine, provide a `MachineReliability(tbf, ttr, initial_tbf_remaining)` or columns `tbf`, `ttr`, and `initial_tbf_remaining` in the station table. Each value can be fixed or sampled from an explicitly supplied `numpy.random.Generator` callable. The failure clock runs only during operations, including setup. A failure that falls during an operation begins at completion. Repair time then advances on the calendar.

Use `Parameter(nominal, sampler)` to configure variable supplier lead time. Supply `InventoryRules(sample_otp=True)` to enable a configured placement-interval sampler in the risk run. Other modes use nominal purchasing terms. Baseline and RFQ Monte Carlo pairs use common per-entity random streams. A change in workload can move the calendar time of the same operating-time failure threshold.

`analyze_risk(experiment, order_id)` reports Pearson and Spearman correlations and regression diagnostics for the selected order's completion, tardiness, or waiting time. The default machine predictors use draws within a common reference budget of scheduled operating minutes. Set `machine_exposure='realized'` or `'both'` to inspect actual outages within the fixed calendar window. Realized outages depend on which work reaches a station. Supplier predictors use purchases placed within the fixed observation window, including purchases made after production finished. Those extra draws and events are marked `after_production`, and the final factory snapshot stays at production end. These associations depend on the chosen observation window and are sensitive to censored outcomes.

## Validation findings

At a 25% target loss, 30 replications per case with seed `2026` produced 23.7% realized generic loss for 18-minute operations, 12.9% for 72-minute operations, and 10.5% for 90-minute operations. The longer operations gave the generator fewer eligible decision points. The validation command also varies workstation count, lot quantity, dispatch policy, and Pareto shape, and prints observed capacity, unused budgets, and target-order delivery distributions. These synthetic results measure the behavior of the proposed rule. Assessing delivery accuracy for a factory requires its own operating data.

The existing `Factory_Model.xlsx` and prototype scripts use a legacy workbook layout. `load_workbook` expects the FACTS V0 sheets above.

## Integrated RFQ decision support

`facts.assess_rfq` uses the factory snapshot and planner efficiency probabilities to produce a recommended commitment, capped Pareto sensitivity, and a matched assessment of existing commitments. The report also identifies queue constraints and ranks explicit machine and supplier risks through paired controls. [Workflow and configuration](docs/rfq_decision.md) · [Synthetic report](validation/rfq_decision/example/decision.md)

### Run an RFQ assessment

Run the synthetic job-shop example from the repository root.

```powershell
.\.venv\Scripts\python.exe -m examples.rfq_decision --output rfq-output
```

For your own assessment, pass the six factory tables, the starting snapshot, and a one-row `Orders` table for the RFQ. Each order line is a separate job. The Python example below supplies those inputs from the synthetic fixture. Replace them with your own factory data, snapshot, and prospective order.

```python
from examples.rfq_decision import HORIZON, decision_factory
from facts import PlannerEfficiency, RFQSettings, assess_rfq, write_rfq_report

data, initial, rfq, risk = decision_factory()
planner = PlannerEfficiency(estimate=.75, certainty="medium", minimum=.65, maximum=.85)
settings = RFQSettings(
    until=HORIZON,
    seed=20261120,
    commitment_probability=.90,
    dispatch_cutoff="14:30",
    practical_delay_minutes=45,
)
result = assess_rfq(data, initial, rfq, planner=planner, settings=settings, risk=risk)
write_rfq_report(result, "rfq-output")
```

Open `rfq-output/decision.md` for the planner report. The same directory contains `decision.json` for integration and compressed simulation traces. The report compares the RFQ with the original backlog under matched random streams and shows which existing commitments are at risk.

### Modeling assumptions

- **Planner efficiency.** Each trajectory draws one efficiency and keeps it for that run. `PlannerEfficiency` puts probability on whole percentages within the supplied bounds. The default half-weight distances from the estimate are 8, 4, and 2 percentage points for low, medium, and high certainty. You can configure these distances or supply explicit weights.
- **Downtime.** The RFQ forecast draws a Poisson count after each completed operation and sums the independent capped Pareto durations. Each draw has a one-working-minute minimum and a 480-working-minute cap. A combined outage can exceed 480 minutes. Downtime pauses during closures. The workflow defaults to immediate activation and supports `idle_policy="next_assignment"`. The low-level simulator's generic default is the shift-budget model. `PoissonEfficiencySettings` selects the Poisson model explicitly.
- **Commitment.** RFQ production ends when all its jobs finish. The quote uses the empirical P90 of the central 80/20 scenario by default, then applies the working calendar and any dispatch cutoff. The date describes production completion or modeled dispatch. Transit time is outside the model. Unfinished runs retain their probability mass, so the quote can be unavailable when the selected quantile is unresolved.
- **Sensitivity and risk.** The 70/30, 80/20, and 90/10 labels describe the underlying Pareto shapes before capping. Their percentile range measures sensitivity to those assumptions. Monte Carlo intervals describe sampling uncertainty separately. Explicit machine failure and supplier lead-time controls run in a separate analysis. The quote comes only from the generic-efficiency forecast.

### RFQ validation

The integrated validation passed 43 core tests and 28 earlier validation checks. It reproduced all 913 placements across 24 archived deterministic schedules. Four capped-Poisson replays matched the archived V2 order-completion timestamps. Controlled cases verified capacity waits, material shortages, interacting risks, censoring, and delays to existing orders. [Validation report](validation/rfq_decision/validation_report.md) · [Verification record](validation/rfq_decision/verification.json)

The published two-job example used 2,000 trajectories per Pareto shape. The central forecast recommended dispatch at about **12:46 on 16 November 2026**, with **98.1%** modeled probability of completing production by 15:00. Its P90 ranged from about **12:44 to 12:52** across the three assumptions. These are synthetic results. Delivery accuracy against real factory records, the planner certainty weights, and the downtime assumptions still need empirical validation. [Example report](validation/rfq_decision/example/decision.md) · [Structured results](validation/rfq_decision/example/decision.json)
