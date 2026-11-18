# FACTS

FACTS helps high-mix, low-volume manufacturers decide what date to quote for a request for quotation (RFQ) and how accepting it would affect existing orders. It simulates the RFQ alongside the current backlog using the factory's shifts, routing, capacity, and stock. The decision report provides a recommended commitment, the probability of meeting the requested date, and the production and supply risks behind the forecast.

## Quickstart

Use Python 3.13 and run the synthetic job-shop example from the repository root.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m examples.rfq_decision --output rfq-output
```

Open `rfq-output/decision.md` for the report. The same directory contains `decision.json` for integration, run metadata, and compressed simulation traces. [Example decision report](validation/rfq_decision/example/decision.md) · [Structured example](validation/rfq_decision/example/decision.json)

## Assess an RFQ

1. Supply the factory tables, a snapshot of the backlog, work in progress, stock and pending deliveries, and a one-row `Orders` table for the RFQ. Each order line is one job. [Factory data schema](docs/simulation_reference.md#input-tables)
2. Set the planner's efficiency estimate and plausible bounds, the simulation horizon, and the commitment percentile. Supply a dispatch cutoff and machine or supplier risk inputs as applicable. [RFQ configuration](docs/rfq_decision.md)
3. Run `assess_rfq` and review the recommended commitment, sensitivity to downtime assumptions, bottlenecks, and matched machine and supplier risks. The report compares the factory with and without the RFQ to show which existing orders are likely to finish late.

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
decision = assess_rfq(data, initial, rfq, planner=planner, settings=settings, risk=risk)
write_rfq_report(decision, "rfq-output")
```

## Modeling assumptions

- **Planner efficiency.** Each trajectory draws one efficiency and keeps it for the run. `PlannerEfficiency` assigns probability to whole percentages within the supplied bounds. Its default half-weight distances from the estimate are 8, 4, and 2 percentage points for low, medium, and high certainty. The distances are configurable, and planners can supply explicit weights.
- **Capacity loss.** The forecast draws a Poisson count after a completed operation and sums independent Pareto durations. Each draw is capped at 480 working minutes and has a one-working-minute minimum. Combined downtime can exceed 480 minutes and pauses during factory closures. The 70/30, 80/20, and 90/10 cases use different assumptions about how much downtime comes from the longest interruptions.
- **Commitment.** The RFQ finishes when all its jobs finish. The default commitment uses the empirical P90 completion time under the central 80/20 assumption and applies any dispatch cutoff against the working calendar. With a cutoff, the date describes modeled dispatch. Transit time is outside the model. Unfinished runs retain their probability mass and can leave the commitment unresolved at the simulation horizon.
- **Risk and sensitivity.** The spread across Pareto assumptions describes model sensitivity. Monte Carlo intervals describe sampling uncertainty separately. FACTS estimates machine and supplier effects through matched controls, and uses the generic-efficiency forecast to calculate the quote. Matched simulations with and without the RFQ measure the effect on existing orders.

The lower-level `simulate`, `compare_rfq`, and `monte_carlo` APIs support individual engine runs and comparisons. [Simulator reference](docs/simulation_reference.md#simulator-api)

## Validation

All **71 tests** passed in the integrated validation, including 43 core tests and 28 historical validation checks. All **913 operation placements** across 24 archived deterministic schedules matched, and four capped-Poisson replays reproduced the archived order-completion timestamps. Controlled cases covered capacity waits, material shortages, interacting risks, censoring, and delays to existing orders. [Validation report](validation/rfq_decision/validation_report.md) · [Verification record](validation/rfq_decision/verification.json)

In the published two-job example, the central forecast recommended dispatch at approximately **12:46 on 16 November 2026** and estimated a **98.1%** chance of completing production by 15:00. P90 ranged from about **12:44 to 12:52** across the three Pareto assumptions. These results come from a synthetic factory. Delivery accuracy, the planner certainty weights, and the downtime assumptions still need validation against real factory records. [Example decision report](validation/rfq_decision/example/decision.md)

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
