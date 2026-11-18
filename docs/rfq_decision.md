# Assess an RFQ

Use `facts.assess_rfq` for one prospective order and its effect on the current backlog. Prepare the six factory tables, the starting snapshot, and a one-row `Orders` table for the RFQ. Each line is one job. [Factory tables and starting snapshot](simulation_reference.md#input-tables)

Set the planner's efficiency estimates in `PlannerEfficiency` and the forecast horizon in `RFQSettings`. The horizon must follow both the snapshot and the RFQ release. Set an observation cutoff within that horizon for explicit risks, or use the forecast cutoff. Supply `InventoryRules` for a custom material dispatch or supplier selection policy.

This example uses the same fixture as the repository quickstart. For your own factory, supply your factory data, snapshot, RFQ table, and any risk settings.

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

Read `decision.md` for the commitment, sensitivity ranges, bottlenecks, risk rankings, and the expected impact on existing orders. `decision.json` contains the structured results. The compressed trajectory tables support a closer look at unfinished runs, resource waits, and matched comparisons.

## Commitment and Pareto sensitivity

Planner probabilities are evaluated at whole percentages inside the supplied bounds. After normalization, the relative weight at a distance of `d` percentage points is `2^(-(d/w)^2)`. The configurable half-weight distances `w` are 8, 4 and 2 percentage points for low, medium and high certainty. This is a planner policy. A caller can pass `half_weight_pp` with their own mapping or use the existing explicit probabilities mapping or `Efficiency_Beliefs` DataFrame. The workflow draws one positive efficiency per run. The run retains that efficiency while its outages vary. The analysis samples from the probabilities directly and gives each sampled trajectory equal mass.

The full productive operation, including setup, finishes before an interruption is generated. For assumed efficiency `η` and productive duration `d`, the Poisson mean is `d × (1 − η) / (η × E[D])`. The underlying Pareto Type I law starts at one working minute. Its shape is `1 / (1 − log(s)/log(.2))` for top-20% share `s`. Each draw is capped at 480 working minutes. The mean integrates the capped survival curve and is approximately 4.913 minutes for 80/20. Summing all elementary draws gives one unavailable episode, which can exceed 480 minutes. The clock pauses through closures, and elapsed work in existing WIP starts at the snapshot. Immediate activation can overlap time that would otherwise be idle. Set `idle_policy="next_assignment"` to queue the already drawn loss until the scheduler would assign that station a job.

The workflow reuses the same sampled planner efficiencies and station seed streams for capped 70/30, 80/20 and 90/10. Its assumption ranges use the minimum and maximum identified percentile over those three runs. The date probability range includes any censoring bounds. Each shape carries separate Monte Carlo intervals. The Pareto labels describe the unbounded base law. The ranges carry no prediction coverage.

The baseline quote is the configured empirical quantile of central 80/20 production completion. Production completion is the maximum of the constituent job completion times when all jobs finish. Calendar and daily dispatch-cutoff rules find the earliest admissible working time on or after that quantile. With a cutoff, the quote describes modeled dispatch. Transportation time is outside the model. A result with an unidentified selected quantile or unavailable dispatch slot has no recommended commitment.

Unfinished runs keep their mass. Quantile endpoints use each run's observed stop and the unknown upper completion bound. An unknown quantile, mean or standard deviation is represented by `null`. Completed-only moments and a lower bound for the mean remain available. Production on-time probability uses the requested timestamp inclusively and reports bounds if a run stops before the requested date. Confidence intervals use empirical binomial order statistics for percentiles and Wilson scores for probabilities. These intervals describe simulation sampling uncertainty.

## Machine and supplier risks

The nominal reference runs with ideal generic efficiency, the original calendar, existing backlog and stock, and nominal lead times. The explicit-risk run activates the configured machine reliability and supplier DLT sampling. Both use internal lead time measured from RFQ release to completion of its last job. Risk results feed the separate risk section of the report. The commitment is calculated exclusively from the generic-efficiency trajectories.

Each configured machine is controlled by suppressing modeled failures, with initial observed outages still represented by the snapshot. Each material and supplier is controlled by using its nominal DLT. Other stations, suppliers and order-interval streams retain their own seeds and draw sequences. Common station streams also pair accepted and baseline runs. Events and consumption can diverge when assignments change.

The signed difference is full-risk lead time minus entity-controlled lead time. Entities are ranked by mean effect among complete pairs. The report flags a critical entity when its mean is positive and the lower empirical probability of delay exceeding `practical_delay_minutes` meets `critical_probability`. Defaults are 60 minutes and 20%. `min_complete_pairs` and `min_complete_fraction` apply to the ranking, and a supplier without sampled exposure is marked as insufficient data. Machine outages, supplier DLTs and stock waits support interpretation. Pearson and Spearman correlations are exploratory.

A double control measures `full − first-controlled − second-controlled + both-controlled` for ranked pairs. The report also records total nominal-to-full effect minus individual marginal effects on common complete runs. Marginal effects can overlap. Control up to `max_interaction_pairs` ranked pairs, default 3, to investigate overlaps. If stochastic ordering cadence is enabled, the total remainder can also include that uncontrolled cadence.

## Queue pressure and existing orders

The observer integrates queue and WIP over actual working time. Capacity wait requires stock, enough remaining shift time for that operation, and no available fitting station on its route. Groups with the most factory capacity-wait job-minutes and most RFQ-specific capacity-wait minutes are reported separately. Queue pressure can appear against several compatible resources. Alternative-resource exposures overlap. Utilization, idle, downtime, operation counts and observed capacity come from the DES station tables.

For each accepted central trajectory, a matched run starts from the same snapshot without the RFQ. Per-order changes include the signed completion difference, the difference of marginal P90 completion timestamps, the P90 of paired completion changes and on-time and miss-probability changes. The same practical-delay and probability thresholds flag existing orders. Raw pairs and probability bounds retain unfinished runs.

## Validation

Synthetic cases cover queue waits, material shortages, interacting risks, and delays to existing orders. The published example records the seed, planner settings, observation cutoff, and source revision. Quoted delivery accuracy, planner certainty weights, and the downtime assumptions still require evidence from real factories. [Validation report](../validation/rfq_decision/validation_report.md) · [Example decision](../validation/rfq_decision/example/decision.md)
