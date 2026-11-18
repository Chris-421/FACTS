# RFQ implementation audit

The working branch is `feature/rfq-decision-support`, based on validated FACTS commit `6dd507fe02ed6d9ff9a3f11cda1e39546eed6e19`. The original 25 core tests and 28 validation tests passed before implementation. Historical results and prior document prefixes are pinned in [the preservation manifest](preserved_baseline.json).

| Status | Findings and implementation scope |
|---|---|
| Implemented and reusable | The shared DES owns calendars, non-preemptive dispatch, routing, workstation state, consumption and purchasing. `simulate` exposes deterministic, generic-efficiency and explicit-risk modes. `compare_rfq` and `monte_carlo` already support paired experiments and per-entity streams. The delivery summarizer keeps censored probability mass and combines planner-weighted samples. Risk logs and `analyze_risk` support association diagnostics. |
| Implemented but incomplete | The one-minute, 480-minute elementary cap and its recalibrated Poisson intensity were tested in the frozen V2 validation study. That implementation is experimental and isolated. The production workflow needs an explicit Poisson setting, sampled planner weights, P80 and uncertainty, and a practical commitment policy. The existing risk summary groups supplier draws and does not estimate entity-controlled effects. Station metrics distinguish utilization and idle categories, with no time-weighted job queue or direct RFQ waiting exposures. |
| Missing | A single decision-support entry point, report and trace export, capped-shape sensitivity ranges, keyed machine and supplier controls, a check for interacting effects, and paired existing-order impact summaries that distinguish the marginal P90 shift from the P90 of paired changes. |

The new entry point uses one normalized snapshot to fork matched factory runs. Its delivery forecast uses generic efficiency. Its explicit-risk analysis uses nominal processing and purchasing as the reference and reports separate entity effects. The original simulation defaults and prior experimental files remain pinned.
