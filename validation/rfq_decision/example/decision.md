# FACTS RFQ decision · RFQ-21

## A. Recommended commitment

**Recommended modeled dispatch · 2026-11-16 12:45:52.314058716**

The 90% policy uses the planner-weighted, capped 80/20 production forecast. The production quantile is 2026-11-16 12:45:52. The requested date is 2026-11-16 15:00:00. Modeled production on-time probability is 98.1%. 2,000 of 2,000 trajectories completed by their observation stop.

Transportation is outside the model. Quantile intervals describe Monte Carlo sampling uncertainty.

Commitment quantile 95% Monte Carlo interval **2026-11-16 12:41:14 to 2026-11-16 12:50:45**. Mean production completion 2026-11-16 12:35:57. Completion standard deviation 172.9 minutes.
With the configured cutoff, modeled dispatch on-time probability is 97.8%.

Planner estimate 75% with medium certainty and 65%–85% plausible bounds. The probability curve uses a 4-point half-weight distance.

## B. Generic efficiency sensitivity

| Underlying Pareto | P50 | P80 | P90 | P95 | Mean completion | SD (min) | On time |
|---|---|---|---|---|---|---:|---:|
| 70/30 | 2026-11-16 12:09:12 | 2026-11-16 12:26:26 | 2026-11-16 12:43:52 | 2026-11-16 13:13:45 | 2026-11-16 12:26:19 | 118.9 | 98.9% |
| 80/20 | 2026-11-16 12:06:23 | 2026-11-16 12:26:53 | 2026-11-16 12:45:52 | 2026-11-16 13:14:44 | 2026-11-16 12:35:57 | 172.9 | 98.1% |
| 90/10 | 2026-11-16 12:04:21 | 2026-11-16 12:28:50 | 2026-11-16 12:52:14 | 2026-11-16 13:34:16 | 2026-11-16 12:41:50 | 197.0 | 97.3% |

P90 assumption range **2026-11-16 12:43:52 to 2026-11-16 12:52:14**. P95 range **2026-11-16 13:13:45 to 2026-11-16 13:34:16**. On-time probability range **97.3%–98.9%**. These ranges reflect the tested assumptions and carry no forecast coverage level. The JSON includes separate sampling intervals for each percentile and shape.

## C. Production constraints

Main factory queue constraint **punching**. Main RFQ capacity wait **milling**.

| Group | Utilization | Mean queue | Mean RFQ queue (working min) | RFQ capacity wait (working min) |
|---|---:|---:|---:|---:|
| milling | 47.9% | 0.7 | 46.5 | 46.3 |
| finishing | 72.4% | 0.6 | 24.1 | 22.9 |
| punching | 26.7% | 0.9 | 0.0 | 0.0 |

## D. Critical operational risks

Nominal reference RFQ lead time **205.0 minutes** from release. This section measures explicit disruptions with generic efficiency fixed at 100% and has no role in the quote.

Critical entities **alloy / Metals Co, Booth B**.

| Entity | Assessment | Complete-pair mean LT effect (min) | P90 paired effect (min) | Material-delay probability | P90 exposure (min) | Pearson/Spearman |
|---|---|---:|---:|---:|---:|---|
| alloy / Metals Co | critical supplier | +428.3 | +1,274.6 | 72.4% | 379.6 | +0.80 / +0.94 (n=500) |
| Booth B | critical machine availability | +196.5 | +1,087.2 | 34.4% | 240.0 | +0.72 / +0.59 (n=500) |
| Punch C | low observed sensitivity | +0.0 | +0.0 | 0.0% | 60.0 | — / — (n=500) |

The ranking uses paired entity controls. Correlations describe exposure and lead time among observed outcomes. Marginal effects can overlap.

alloy / Metals Co and Booth B showed a mean interaction of +128.9 minutes across 500 complete paired sets.

## E. Existing commitments

**2** existing orders met the flag threshold of a 20% chance of delay exceeding 45 minutes.

| Order | Mean paired change (min) | Change in scenario P90 (min) | P90 of paired changes (min) | Miss-probability change (pp) |
|---|---:|---:|---:|---:|
| E-02 | +204.3 | +208.2 | +218.6 | +5.8 |
| E-03 | +311.0 | +1,180.6 | +1,225.3 | +3.1 |
| E-01 | +33.4 | +33.7 | +33.7 | +0.7 |
| SIDE | +0.0 | +0.0 | +0.0 | +0.0 |

Seed 20261120. Observation cutoff 2026-11-24 16:00:00. The quote is conditional on the model and has yet to be validated against real HMLV completions.
