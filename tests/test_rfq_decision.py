"""Controlled end-to-end RFQ scenarios and statistical edge cases."""

import gzip
import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import Mock

import numpy as np
import pandas as pd

from examples.rfq_decision import decision_factory
from examples.synthetic import frame, weekly
from facts import (EfficiencySettings, FactoryData, InventoryRules, MachineReliability, Parameter, ParetoDowntime,
                   PlannerEfficiency, PoissonEfficiencySettings, RFQSettings, RiskSettings, RunLimits, Snapshot,
                   assess_rfq, compare_rfq, simulate, write_rfq_report)
from facts.calendar import ShiftCalendar
from facts.experiments import sample_efficiency_trials
from facts.forecast import CompletionSamples, paired_effect
from facts.model import Model
from facts.rfq import practical_dispatch
from facts.sensitivity import association
from facts.simulation import Simulation


START = pd.Timestamp("2026-11-16 08:00")
END = START + pd.Timedelta(days=2, hours=8)


def at(value):
    return START + pd.Timedelta(minutes=value)


def order(key, part="item", release=0, due=480):
    return key, [(part, 1)], at(release), at(due), "outstanding"


def fixture(*, existing=(), rfq_release=0, rfq_due=360, kit=(), machines=(), rule="FIFO", duration=60):
    materials = [[material, "stock", [supplier], 10, dlt, 1440, None] for material, supplier, dlt, _ in kit]
    other_parts = [("old", ["main"], {})]
    processes = [("main", duration, 0, ["line"])]
    stations = [("A", "line", True, None)]
    if "side" in machines:
        other_parts.append(("side", ["side-op"], {}))
        processes.append(("side-op", 10, 0, ["independent"]))
        stations.append(("C", "independent", True, None))
    part = ("item", ["main"], {0: {material: 1 for material, *_ in kit}})
    data = FactoryData(
        frame("FACTS", [("F", "RFQ fixture", "test", 1, weekly(), {}, rule, {})]),
        frame("Parts", [part, *other_parts]), frame("Processes", processes), frame("Workstations", stations),
        frame("Orders", existing), frame("Materials", materials),
    )
    stock = pd.DataFrame([{"ID_mat": material, "cq": quantity, "next_order_time": START}
                          for material, _, _, quantity in kit]) if kit else None
    return data, Snapshot(START, inventory=stock), frame("Orders", [order("R", "item", rfq_release, rfq_due)])


def assess(inputs, *, risk=None, planner=None, **settings):
    defaults = dict(until=END, runs=6, risk_runs=8, seed=514, practical_delay_minutes=15,
                    min_complete_pairs=3, max_interaction_pairs=1)
    defaults.update(settings)
    return assess_rfq(*inputs, planner={1.: 1.} if planner is None else planner, settings=RFQSettings(**defaults), risk=risk)


class PlannerAndForecastTests(unittest.TestCase):
    def test_planner_bounds_certainty_and_weighted_draws(self):
        spread = []
        for certainty in ("low", "medium", "high"):
            weights = PlannerEfficiency(.75, certainty, .65, .85).weights()
            self.assertEqual(list(weights), [i / 100 for i in range(65, 86)])
            self.assertAlmostEqual(sum(weights.values()), 1.)
            self.assertAlmostEqual(sum(k*v for k, v in weights.items()), .75)
            self.assertEqual(max(weights, key=weights.get), .75)
            spread.append(sum((k-.75)**2*v for k, v in weights.items()))
        self.assertGreater(spread[0], spread[1])
        self.assertGreater(spread[1], spread[2])
        narrowed = PlannerEfficiency(.75, "medium", .651, .809, {"medium": 1.5}).weights()
        self.assertEqual((min(narrowed), max(narrowed)), (.66, .80))
        for case in (PlannerEfficiency(.75, "unknown", .65, .85), PlannerEfficiency(.95, "low", .65, .85),
                     PlannerEfficiency(.755, "low", .754, .756), PlannerEfficiency(.75, "medium", .65, .85, {"medium": 0})):
            with self.assertRaises(ValueError):
                case.weights()
        first = sample_efficiency_trials({.7: .25, .8: .75}, 4000, 22)
        self.assertEqual(first, sample_efficiency_trials({.7: .25, .8: .75}, 4000, 22))
        self.assertAlmostEqual(sum(eta == .8 for eta, _ in first) / len(first), .75, delta=.03)
        self.assertEqual(len({seed for _, seed in first}), len(first))

    def test_ranked_dates_and_censoring_keep_full_mass(self):
        sample = CompletionSamples([(at(10), at(60)), (at(30), at(60)), (None, at(25)), (None, at(80))], START)
        self.assertEqual(sample.quantile(.50)["earliest"], at(25))
        self.assertEqual(sample.quantile(.50)["latest"], at(30))
        self.assertIsNone(sample.quantile(.50)["date"])
        summary = sample.summary(at(60))
        self.assertEqual(summary["censored"], 2)
        self.assertEqual(summary["completed_only_mean"], at(20))
        self.assertIsNone(summary["mean_completion"])
        self.assertEqual((summary["on_time"]["lower"], summary["on_time"]["upper"]), (.50, .75))
        full = CompletionSamples([(at(value), at(100)) for value in range(1, 101)], START)
        self.assertEqual(full.summary(at(100))["quantiles"]["p80"]["date"], at(80))

    def test_paired_p90_and_marginal_p90_are_different(self):
        a = CompletionSamples([(at(i), at(200)) for i in [0]*8 + [100]*2], START)
        b = CompletionSamples([(at(i), at(200)) for i in [100]*2 + [0]*8], START)
        effect = paired_effect(b, a, 25)
        self.assertEqual(a.quantile(.9)["date"], b.quantile(.9)["date"])
        self.assertEqual(effect["p90_minutes"]["estimate"], 100)
        self.assertEqual(effect["mean_minutes"], 0)
        self.assertEqual(effect["positive"]["estimate"], .2)

    def test_shipping_rolls_holidays_and_respects_cutoff(self):
        cal = ShiftCalendar(weekly(), {"2026-11-23": []})
        friday = pd.Timestamp("2026-11-20 15:30")
        cutoff = pd.Timestamp("2026-11-20 15:00").time()
        self.assertEqual(practical_dispatch(cal, friday, cutoff), pd.Timestamp("2026-11-24 08:00"))
        self.assertEqual(practical_dispatch(cal, friday, None), friday)
        exact = pd.Timestamp("2026-11-20 16:00")
        self.assertEqual(practical_dispatch(cal, exact, None), exact)

    def test_association_reports_sample_size_and_constant_predictors(self):
        values = association([1, 2, 3, None], [14, 12, 10, 2])
        self.assertEqual(values["n"], 3)
        self.assertAlmostEqual(values["pearson"], -1.)
        self.assertAlmostEqual(values["spearman"], -1.)
        self.assertIsNone(association([1, 1], [3, 2])["pearson"])


class PoissonWorkflowTests(unittest.TestCase):
    def test_capped_law_and_exact_trigger(self):
        for share in (.7, .8, .9):
            law = ParetoDowntime(top_share=share)
            grid = np.geomspace(1., 480., 10_001)
            mean = 1. + np.trapezoid(grid ** -law.shape, grid)
            self.assertAlmostEqual(law.mean, mean, places=6)
            self.assertAlmostEqual(.2 ** (1 - 1/law.shape), share)
            self.assertAlmostEqual(PoissonEfficiencySettings(.75, law).rate * law.mean, 1/3)
        self.assertAlmostEqual(ParetoDowntime().mean, 4.912780, places=5)

    def test_working_clock_and_compound_cap(self):
        data, initial, rfq = fixture(existing=[order("first", "old")], rfq_release=60)
        start = pd.Timestamp("2026-11-20 14:00")
        data.Orders["RD"] = start
        data.Orders["DD"] = start + pd.Timedelta(days=7)
        rfq["RD"] = start + pd.Timedelta(hours=1)
        rfq["DD"] = start + pd.Timedelta(days=7)
        model = Model(data, Snapshot(start), rfq)
        loss = PoissonEfficiencySettings(.75)
        engine = Simulation(model, InventoryRules(), RunLimits(), loss, None, 0, "rfq", 1, None, "efficiency_mc")
        rng = Mock()
        rng.poisson.side_effect = [2, 0]
        rng.pareto.return_value = np.array([999., 59.])
        engine.uncertainty.stations["A"] = rng
        run = engine.run(["R"])
        events = run.event_log
        fail = events.loc[events.event_type.eq("unavailability_start")].iloc[0]
        self.assertEqual(fail.timestamp, start + pd.Timedelta(hours=1))
        self.assertEqual(fail.details["duration_minutes"], 540)
        self.assertEqual(fail.details["underlying_minutes"], (480., 60.))
        self.assertEqual(events.loc[events.event_type.eq("station_recovery"), "timestamp"].item(), pd.Timestamp("2026-11-23 16:00"))
        self.assertEqual(run.orders.set_index("ID_O").loc["R", "completion_time"], pd.Timestamp("2026-11-24 09:00"))
        self.assertEqual(run.stations.generic_unavailability_time.item(), 540.)
        self.assertAlmostEqual(rng.poisson.call_args_list[0].args[0], loss.rate * 60)

    def test_poisson_one_matches_deterministic_and_old_mode_is_available(self):
        data, initial, rfq = fixture()
        old = simulate(data, initial, rfq=rfq, efficiency=EfficiencySettings(.75), seed=22)
        self.assertIn("generic_trigger", old.sampled_inputs.parameter.tolist())
        new = simulate(data, initial, rfq=rfq, efficiency=PoissonEfficiencySettings(1))
        plain = simulate(data, initial, rfq=rfq)
        pd.testing.assert_frame_equal(new.orders, plain.orders)
        self.assertTrue(new.sampled_inputs.empty)

    def test_deferred_and_immediate_starts_differ_when_work_is_absent(self):
        data, initial, rfq = fixture(existing=[order("first", "old")], rfq_release=180)
        for name, started, last in (("immediate", 60, 240), ("next_assignment", 180, 300)):
            cfg = PoissonEfficiencySettings(.75, idle_policy=name)
            engine = Simulation(Model(data, initial, rfq), InventoryRules(), RunLimits(), cfg, None, 0, "rfq", 1, None, "efficiency_mc")
            rng = Mock()
            rng.poisson.side_effect = [1, 0]
            rng.pareto.return_value = np.array([59.])
            engine.uncertainty.stations["A"] = rng
            result = engine.run(["R"])
            row = result.event_log.loc[result.event_log.event_type.eq("unavailability_start")]
            self.assertEqual(row.timestamp.item(), at(started))
            self.assertEqual(result.orders.set_index("ID_O").loc["R", "completion_time"], at(last))


class DecisionScenarioTests(unittest.TestCase):
    def test_single_machine_queue_and_station_utilization_reconcile(self):
        inputs = fixture(existing=[order("E1", "old"), order("E2", "old")], rfq_release=30)
        result = assess(inputs)
        line = next(row for row in result.report["capacity"]["resources"] if row["resource_type"] == "group")
        self.assertEqual(result.report["capacity"]["main_rfq_constraint"], "line")
        self.assertEqual(line["processing_utilization"], 1.)
        self.assertAlmostEqual(line["mean_queue_length"], 150 / 180)
        self.assertEqual(line["mean_rfq_capacity_wait_minutes"], 90.)
        self.assertEqual(line["max_sampled_queue"], 2)
        checked = compare_rfq(*inputs)
        self.assertEqual(result.report["commitment"]["recommended_production_completion"], checked.accepted.experiment.rfq_completion_time.item())
        initial_compare = checked.baseline.initial["S"]
        pd.testing.assert_frame_equal(initial_compare.drop(columns="state"), checked.accepted.initial["S"].drop(columns="state"))

    def test_frequent_off_route_failure_and_high_load_on_route(self):
        work = [order("existing", "old")] + [order(f"side{i}", "side") for i in range(8)]
        inputs = fixture(existing=work, machines=("side",), rfq_release=25)
        risk = RiskSettings(END, {"A": MachineReliability(500, 60, 10), "C": MachineReliability(12, 5, 2)})
        result = assess(inputs, risk=risk)
        table = result.report["operational_risk"]["rankings"]
        self.assertEqual(table[0]["entity"], "A")
        self.assertEqual(table[0]["mean_minutes"], 60)
        self.assertEqual(table[0]["label"], "critical machine availability")
        self.assertEqual(table[1]["entity"], "C")
        self.assertGreater(table[1]["exposures"]["episode_count"]["mean"], table[0]["exposures"]["episode_count"]["mean"])
        self.assertEqual(table[1]["mean_minutes"], 0)
        self.assertEqual(table[1]["label"], "low observed sensitivity")
        self.assertGreater(result.traces["machine_samples"].parameter.eq("repair_minutes").sum(), 0)
        self.assertEqual(set(result.traces["machine_episodes"].station), {"A", "C"})
        pd.testing.assert_frame_equal(result.traces["risk_pairs"], assess(inputs, risk=risk).traces["risk_pairs"])

    def test_variable_delivery_with_stock_and_a_direct_material_gate(self):
        terms = Parameter(30, lambda rng: rng.uniform(120, 150))
        loaded = assess(fixture(kit=(("metal", "S", terms, 10),)))
        ranked = loaded.report["operational_risk"]["rankings"][0]
        self.assertEqual(ranked["label"], "low observed sensitivity")
        self.assertEqual(ranked["mean_minutes"], 0.)
        dry = assess(fixture(kit=(("metal", "S", terms, 0),)))
        right = dry.report["operational_risk"]["rankings"][0]
        self.assertEqual(right["entity"], ("metal", "S"))
        self.assertEqual(right["label"], "critical supplier")
        self.assertGreater(right["mean_minutes"], 90.)
        self.assertAlmostEqual(right["correlations"]["mean_dlt_minutes"]["pearson"], 1.)
        supplier = dry.traces["supplier_deliveries"].query('placed_before_rfq_window_end == True')
        self.assertTrue(supplier.nominal_dlt_minutes.eq(30).all())
        self.assertTrue(supplier.sampled_dlt_minutes.ge(120).all())
        self.assertTrue(supplier.actual_arrival.notna().all())
        self.assertTrue(supplier.rfq_jobs_short_at_arrival.map(bool).all())
        requirements = dry.traces["material_requirements"]
        self.assertGreater(requirements.short_wait_working_minutes.sum(), 0.)
        self.assertEqual(dry.report["operational_risk"]["reference"]["lead_minutes"], 90)
        self.assertEqual(dry.report["commitment"]["recommended_production_completion"], at(90))

    def test_competing_suppliers_and_interaction_are_signed(self):
        inputs = fixture(kit=(("x", "S1", Parameter(20, lambda rng: 120), 0),
                              ("y", "S2", Parameter(30, lambda rng: 90), 0)), duration=30)
        result = assess(inputs)
        table = result.report["operational_risk"]["rankings"]
        self.assertEqual([(row["entity"], row["mean_minutes"]) for row in table], [(("x", "S1"), 30), (("y", "S2"), 0)])
        self.assertEqual(result.report["operational_risk"]["interactions"][0]["mean_minutes"], -60)
        self.assertEqual(result.report["operational_risk"]["full_minus_nominal"]["mean_minutes"], 90)
        self.assertEqual(result.report["operational_risk"]["total_minus_sum_of_marginals"]["mean_minutes"], 60)
        self.assertTrue(result.report["operational_risk"]["interactions"][0]["nonzero_mc95"])

    def test_supplier_control_keeps_other_supplier_draw_stream(self):
        data, initial, rfq = fixture(kit=(("x", "S1", Parameter(20, lambda rng: rng.uniform(20, 150)), 10),))
        row = data.Materials.copy()
        first = Parameter(20, lambda rng: rng.uniform(20, 50))
        second = Parameter(40, lambda rng: rng.uniform(40, 100))
        row.at[0, "suppliers"] = ["S1", "S2"]
        row.at[0, "DLT"] = {"S1": first, "S2": second}
        row.at[0, "OTP"] = 15
        data = replace(data, Materials=row)
        inventory = InventoryRules(select_supplier=lambda key, suppliers, now: suppliers[int((now - START).total_seconds() // 900) % 2])
        full = RiskSettings(at(150), matched_supply_streams=True)
        controlled = replace(full, controlled_suppliers=frozenset({("x", "S1")}))
        runs = [simulate(data, initial, rfq=rfq, risk=item, inventory=inventory, seed=99) for item in (full, controlled)]
        second_series = [run.sampled_inputs.loc[run.sampled_inputs.entity_id.eq("S2"), "sampled_value"].tolist() for run in runs]
        self.assertGreater(len(second_series[0]), 2)
        self.assertEqual(*second_series)
        self.assertFalse(runs[1].sampled_inputs.entity_id.eq("S1").any())

    def test_rfqs_that_delay_existing_work_and_rfqs_that_do_not(self):
        baseline = [order("E", "old", due=90)]
        delayed = assess(fixture(existing=baseline, rfq_due=75, rule="EDD"))
        impact = delayed.report["existing_order_impact"]
        row = impact["orders"][0]
        self.assertEqual(impact["materially_delayed_orders"], 1)
        self.assertEqual(row["mean_minutes"], 60)
        self.assertEqual(row["marginal_p90_change_minutes"], 60)
        self.assertEqual(row["p90_minutes"]["estimate"], 60)
        self.assertEqual(row["miss_probability_change"]["estimate"], 1)
        harmless = assess(fixture(existing=baseline, rfq_release=150), planner={.75: 1.})
        self.assertEqual(harmless.report["existing_order_impact"]["materially_delayed_orders"], 0)
        self.assertTrue(harmless.traces["existing_order_pairs"].paired_change_minutes.eq(0.).all())

    def test_forecast_cutoff_leaves_the_commitment_unidentified(self):
        evaluation = assess(fixture(), until=at(30)).report
        self.assertIsNone(evaluation["commitment"]["recommended_commitment"])
        forecast = evaluation["commitment"]["forecast"]
        self.assertEqual(forecast["censored"], 6)
        self.assertIsNone(forecast["quantiles"]["p90"]["date"])
        self.assertEqual(forecast["on_time"]["upper"], 1.)
        self.assertEqual(forecast["on_time"]["lower"], 0.)

    def test_export_multijob_sample_and_sensitivity(self):
        data, initial, rfq, risk = decision_factory()
        result = assess((data, initial, rfq), risk=risk, planner=PlannerEfficiency(.75, "medium", .70, .80))
        paths = result.traces["efficiency_outcomes"]
        jobs = result.traces["rfq_jobs"]
        self.assertEqual(jobs.groupby(["trial", "law"]).size().unique().tolist(), [2])
        pd.testing.assert_series_equal(jobs.groupby(["trial", "law"]).completion.max(),
                                       paths.set_index(["trial", "law"]).completion.sort_index())
        self.assertTrue(paths.groupby("trial").efficiency.nunique().eq(1).all())
        options = result.report["pareto_sensitivity"]["assumptions"]
        dates = [row["forecast"]["quantiles"]["p90"]["date"] for row in options.values()]
        self.assertEqual(result.report["pareto_sensitivity"]["ranges"]["p90"], {"lower": min(dates), "upper": max(dates)})
        with tempfile.TemporaryDirectory() as folder:
            write_rfq_report(result, folder)
            loaded = json.loads((Path(folder) / "decision.json").read_text(encoding="utf-8"))
            md = (Path(folder) / "decision.md").read_text(encoding="utf-8")
            self.assertEqual(loaded["rfq_id"], "RFQ-21")
            self.assertEqual(md.count("\n## "), 5)
            with gzip.open(Path(folder) / "traces/efficiency_outcomes.jsonl.gz", "rt", encoding="utf-8") as stream:
                self.assertEqual(len(stream.readlines()), 18)
