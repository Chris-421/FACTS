# FACTS

FACTS helps high-mix, low-volume manufacturers decide what date to quote for a request for quotation (RFQ) and how accepting it would affect existing orders. It uses the factory's shifts, routing, capacity, and stock to model the RFQ alongside the current backlog. The report gives a recommended commitment, the estimated chance of meeting the requested date, and the factory constraints and supply risks to review.

## Quickstart

Use Python 3.13 and run the synthetic job-shop example from the repository root.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m examples.rfq_decision --output rfq-output
```

Open `rfq-output/decision.md` for the report. The same directory contains `decision.json` for integration, run metadata, and compressed simulation traces. [Example decision report](validation/rfq_decision/example/decision.md) · [Structured example](validation/rfq_decision/example/decision.json)

## Assess an RFQ

1. Prepare the factory tables and a starting snapshot of work in progress, stock, and pending deliveries. Supply a one-row `Orders` table for the RFQ. Each order line is one job. [Factory inputs](docs/simulation_reference.md#input-tables)
2. Set the planner's efficiency estimate, certainty, and plausible bounds, plus the forecast horizon and commitment percentile. Supply a dispatch cutoff and machine or supplier risk inputs as applicable.
3. Call `assess_rfq` and use `write_rfq_report` to save the decision. Review the proposed commitment, sensitivity to downtime assumptions, bottlenecks, machine and supplier effects, and the change in on-time probability for existing orders. [Workflow and Python example](docs/rfq_decision.md)

## Modeling assumptions

- **Efficiency and downtime.** Each trajectory draws one efficiency and keeps it for the run. After each completed operation, FACTS uses a Poisson count to draw Pareto downtimes of 1–480 working minutes each. Their sum is the workstation's outage, which pauses during factory closures and can exceed 480 minutes.
- **Commitment.** The RFQ finishes when all its jobs finish. FACTS defaults to P90 under the central 80/20 Pareto assumption and applies the factory calendar and any dispatch cutoff. With a cutoff, the quote describes modeled dispatch. Transit time is outside the model. Unfinished runs retain their probability mass and can leave the commitment unresolved.
- **Sensitivity and order impact.** The 70/30 and 90/10 cases show how different downtime assumptions affect the forecast. Monte Carlo intervals show sampling uncertainty separately. Matched runs with and without the RFQ measure the effect on current commitments. Explicit machine and supplier risks are estimated in a separate analysis. [Model and report details](docs/rfq_decision.md#commitment-and-pareto-sensitivity)

## Validation

All **71 tests** passed in the integrated validation, including 43 core tests and 28 historical validation checks. All **913 operation placements** across 24 archived deterministic schedules matched, and four capped-Poisson replays reproduced the archived order-completion timestamps. Controlled cases covered capacity waits, material shortages, interacting risks, censoring, and delays to existing orders. [Validation report](validation/rfq_decision/validation_report.md) · [Verification record](validation/rfq_decision/verification.json)

In the published two-job example, the central forecast recommended dispatch at approximately **12:46 on 16 November 2026** and estimated a **98.1%** chance of completing production by 15:00. P90 ranged from about **12:44 to 12:52** across the three Pareto assumptions. These results come from a synthetic factory. Delivery accuracy, the planner certainty weights, and the downtime assumptions still need validation against real factory records. [Example decision report](validation/rfq_decision/example/decision.md)

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
