# Integrated RFQ validation

The implementation was tested on controlled factory snapshots and a multi-job RFQ. The implementation commit, seed, settings and source hashes are recorded in the example run. [Implementation audit](implementation_audit.md) · [Verification record](verification.json) · [Example run metadata](example/run.json)

## Controlled scenarios

| Scenario | Simulated outcome checked |
|---|---|
| Single-machine bottleneck | The route group accrued 90 RFQ queue minutes over 180 observed working minutes. Mean queue length was 150 job-minutes divided by 180 observed minutes, and utilization was 100%. |
| Two machines with one required route | The required machine added 60 minutes to lead time. The independent machine added zero. |
| Variable supplier DLT with sufficient inventory | The supplier supplied changing lead times and had zero paired effect on RFQ completion. |
| Supplier delivery blocks production | With zero stock the RFQ waited for the timed delivery. The source ranked as critical and had a positive paired effect above 90 minutes in the test. Nominal DLT remained 30 minutes in the reference. |
| Multiple competing material constraints | For a job requiring two materials, controlling the limiting supplier reduced lead time by 30 minutes. Controlling the other alone left completion unchanged. |
| Frequent failures with little RFQ effect | The independent station failed more often in the RFQ observation window and registered zero paired effect. |
| High-utilization machine with material effect | Suppressing the route machine's modeled failure reduced lead time by 60 minutes, and it ranked as critical. |
| Interacting risks | The two supplier gates had a signed double-control interaction of −60 minutes. Their marginal effects summed to 30 minutes, while full risk added 90 minutes over the nominal reference. |
| RFQ delays an existing order | Accepting the earlier-due RFQ moved the existing order 60 minutes later and increased its modeled miss probability by 100 points in the deterministic fixture. |
| RFQ has negligible existing-order impact | An RFQ released after the existing work completed had zero paired effect across the stochastic comparison. |

The new suite also checked planner bounds and certainty, seeded sampling, the recalibrated capped Pareto mean, non-preemption, carryover, deferred activation, P80 and censoring, shipping cutoffs, shared supplier streams, paired versus marginal P90, multi-job completion and JSON and Markdown export.

## Compatibility

The 43 core tests and 28 prior validation tests passed. All 913 placements across the 24 archived deterministic schedules were identical. Four capped-Poisson replays matched the frozen V2 experiments exactly at the order-completion timestamps, including immediate and deferred activation. Checksums verified all 234 protected historical files and all three earlier document prefixes. The Codex comparison is part of that archive. [Verification record](verification.json) · [Protected files](preserved_baseline.json)

The first full example exposed a one-nanosecond difference on an independent order in one pair. The working-time clock now measures each outage from its activation or the applicable shift opening. The same-seed case is a regression test, and the independent order has zero paired effect.

## Executed RFQ example

Seed `20261120` generated 2,000 paths per capped Pareto shape, 2,000 matched forecasts without the RFQ, and 500 paths each for full risk, each individual control and three double controls. The example also ran the nominal RFQ reference. The requested production date was 16 November 2026 at 15:00. The 80/20 P90 production completion and the modeled dispatch recommendation were both approximately **12:46** that day. The P90 Monte Carlo interval was approximately **12:41–12:51**. Production on-time probability was **98.1%** and dispatch on-time probability with the 14:30 cutoff was **97.8%**. The 11,501 simulator runs and report export took **88.4 seconds** on this workspace. Central P90 increased by 24 seconds from the first 1,000 trajectories to all 2,000. [Example report](example/decision.md) · [Structured results](example/decision.json) · [Scenario and runtime](example/run.json) · [Trajectory checks](example/checks.json)

The P90 assumption range was **12:43:52–12:52:14**. The production on-time probability range was **97.3%–98.9%**. These ranges summarize the tested capped shapes. Each shape has its own Monte Carlo intervals. Bottleneck observations ranked punching first for overall capacity-wait job-minutes and milling first for RFQ capacity waiting. Finishing had the highest utilization.

Controlling alloy deliveries from Metals Co reduced mean lead time by **428.3 minutes** (95% Monte Carlo interval **379.8–476.8**). Controlling Booth B reduced it by **196.5 minutes** (**161.1–231.9**). Punch C had zero paired impact. The joint Booth B and Metals Co control yielded a **+128.9-minute** signed interaction (**93.1–164.7**). The explicit-risk reference and effects support the vulnerability ranking. They have no role in the quotable date. [Risk outcomes and intervals](example/decision.json)

E-02 and E-03 met the example's material-delay flag. E-02's on-time miss risk increased by **5.8 percentage points**. Its difference between scenario P90 dates was **208.2 minutes**, and its P90 of the matched changes was **218.6 minutes**. The independent SIDE order remained unchanged in every pair.

The synthetic evidence verifies the implementation and its decision outputs. Forecast accuracy, the subjective weighting policy and the modeled disruption distributions still require matched industrial data.
