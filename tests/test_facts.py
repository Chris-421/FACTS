import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from examples.efficiency_validation import Case, validate_efficiency
from examples.synthetic import example_factory, frame, weekly
from facts import (EfficiencySettings, FactoryData, InventoryRules, MachineReliability, Parameter, RiskSettings,
                   RunLimits, Snapshot, analyze_risk, compare_rfq, load_workbook, monte_carlo, simulate)
from facts.calendar import ShiftCalendar
from facts.experiments import summarize_delivery
from facts.model import TABLES
from facts.uncertainty import downtime_probability


MONDAY = pd.Timestamp("2026-11-16 08:00")


def order(order_id, q, release=MONDAY, due=MONDAY + pd.Timedelta(days=5), part="p"):
    return order_id, [(part, q)], release, due, "outstanding"


def single_process(orders, *, duration=30, setup=None, stations=None, calendar=None, exceptions=None,
                   rule="EDD", parts=None, materials=None):
    return FactoryData(
        frame("FACTS", [("F", "Factory", "Test", 1, weekly() if calendar is None else calendar,
                         exceptions or {}, rule, {})]),
        frame("Parts", [("p", ["proc"], {})] if parts is None else parts),
        frame("Processes", [("proc", duration, setup, ["G"])]),
        frame("Workstations", [("S", "G", True, None)]) if stations is None else stations,
        frame("Orders", orders), frame("Materials") if materials is None else materials,
    )


def events(run, event_type):
    return run.event_log.loc[run.event_log.event_type == event_type]


class CalendarTests(unittest.TestCase):
    def test_weekly_rollover_closure_and_multiple_types(self):
        slots = [{"shift_number": 0, "Shift_ID": "morning", "weekday": 0, "start": "08:00", "end": "12:00"},
                 {"shift_number": 1, "Shift_ID": "evening", "weekday": 4, "start": "20:00", "end": "02:00"}]
        calendar = ShiftCalendar(slots, {"2026-11-23": []})
        evening = calendar.next_slot(pd.Timestamp("2026-11-21 00:00"))
        self.assertEqual((evening.shift_number, evening.Shift_ID), (1, "evening"))
        self.assertEqual(evening.t_done, pd.Timestamp("2026-11-21 02:00"))
        self.assertEqual(calendar.next_slot(evening.t_done).t_start, pd.Timestamp("2026-11-27 20:00"))
        self.assertEqual(calendar.next_slot(pd.Timestamp("2026-11-28 02:00")).shift_number, 0)

    def test_replacement_and_overlap_validation(self):
        special = {"shift_number": 0, "Shift_ID": "special", "start": "10:00", "end": "12:00"}
        calendar = ShiftCalendar(weekly(), {"2026-11-16": [special]})
        self.assertEqual(calendar.next_slot(MONDAY).t_start, MONDAY + pd.Timedelta(hours=2))
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            ShiftCalendar(weekly(), {"2026-11-16": [special, dict(special, start="11:00")]})


class DeterministicTests(unittest.TestCase):
    def test_snapshot_routing_rfq_and_timestamps(self):
        data, initial, rfq = example_factory()
        result = compare_rfq(data, initial, rfq)
        first_finish = events(result.accepted, "operation_complete").iloc[0]
        self.assertEqual(first_finish.timestamp, MONDAY + pd.Timedelta(minutes=45))
        self.assertEqual(first_finish.details["next_step"], 1)
        self.assertEqual(result.accepted.experiment.iloc[0].rfq_completion_time, MONDAY + pd.Timedelta(minutes=210))
        self.assertEqual(result.existing_orders.iloc[0].completion_time_change, pd.Timedelta(minutes=90))
        self.assertEqual(result.baseline.final["S"].state.tolist(), ["available", "available"])
        self.assertTrue(result.accepted.event_log.timestamp.is_monotonic_increasing)
        self.assertTrue(result.accepted.event_log.event_id.is_unique)
        self.assertEqual(result.accepted.final["Inv"].iloc[0].pending_deliveries[0]["ID_PO"], "PO-2")
        repeat = compare_rfq(data, initial, rfq)
        pd.testing.assert_frame_equal(result.accepted.event_log, repeat.accepted.event_log)

    def test_completed_orders_and_missing_wip(self):
        data = single_process([order("past", 1, MONDAY - pd.Timedelta(days=1))])
        with self.assertRaisesRegex(ValueError, "Missing WIP"):
            simulate(data, Snapshot(MONDAY))
        data.Orders.loc[0, "status"] = "completed"
        result = simulate(data, Snapshot(MONDAY))
        self.assertTrue(result.jobs.empty)
        self.assertTrue(result.orders.empty)

    def test_missing_known_and_explicit_zero_setup(self):
        for setup, previous, expected, source in [
            (None, "p", 30, "missing_assumed_zero"), (0, "p", 30, "explicit_zero"),
            ({("p", "p"): 8}, "p", 38, "measured"), (100, None, 30, "unknown_previous_setup"),
        ]:
            with self.subTest(source=source):
                data = single_process([order("O", 1)], setup=setup)
                data.Workstations["previous_part"] = [previous]
                run = simulate(data, Snapshot(MONDAY))
                self.assertEqual(run.orders.iloc[0].completion_time, MONDAY + pd.Timedelta(minutes=expected))
                self.assertEqual(events(run, "operation_start").iloc[0].details["setup_source"], source)

    def test_edd_priorities_then_duration_then_queue_entry_then_list(self):
        start = MONDAY - pd.Timedelta(hours=1)
        rows = [order("listed-first", 2, start), order("same-time", 1, start),
                order("earlier-queue", 1, start - pd.Timedelta(minutes=30)), order("urgent", 3, start, MONDAY)]
        run = simulate(single_process(rows), Snapshot(start - pd.Timedelta(hours=1)))
        self.assertEqual(events(run, "operation_start").entity_id.tolist(),
                         [("urgent", 1), ("earlier-queue", 1), ("same-time", 1), ("listed-first", 1)])
        equal = simulate(single_process([order("z", 1), order("a", 1)]), Snapshot(MONDAY))
        self.assertEqual(events(equal, "operation_start").entity_id.tolist(), [("z", 1), ("a", 1)])

    def test_fifo_uses_current_queue_entry(self):
        earlier = MONDAY - pd.Timedelta(hours=2)
        data = single_process([order("A", 1, earlier), order("B", 1, earlier + pd.Timedelta(minutes=15))], rule="FIFO")
        wip = pd.DataFrame([{"ID_O": "A", "line": 1, "state": "waiting", "step": 0,
                             "ready_at": earlier + pd.Timedelta(hours=1)},
                            {"ID_O": "B", "line": 1, "state": "waiting", "step": 0,
                             "ready_at": earlier + pd.Timedelta(minutes=15)}])
        run = simulate(data, Snapshot(MONDAY, wip))
        self.assertEqual(events(run, "operation_start").entity_id.tolist(), [("B", 1), ("A", 1)])

    def test_station_selection_includes_setup(self):
        stations = frame("Workstations", [("first", "G", True, None), ("fast", "G", True, None)])
        stations["previous_part"] = ["old", "p"]
        setup = {("old", "p"): 40, ("p", "p"): 0}
        data = single_process([order("order", 1)], stations=stations, setup=setup)
        run = simulate(data, Snapshot(MONDAY))
        self.assertEqual(events(run, "operation_start").iloc[0].related_entity_id, "fast")

    def test_backfill_and_exact_boundary_completion(self):
        data = single_process([order("occupy", 9, due=MONDAY), order("high", 2, due=MONDAY + pd.Timedelta(hours=8)),
                               order("low", 1)], duration=48)
        run = simulate(data, Snapshot(MONDAY))
        self.assertEqual(events(run, "operation_start").entity_id.tolist(), [("occupy", 1), ("low", 1), ("high", 1)])
        boundary = run.event_log.loc[run.event_log.timestamp == MONDAY + pd.Timedelta(hours=8)]
        self.assertEqual(boundary.event_type.tolist(), ["operation_complete", "station_release", "shift_end"])

    def test_permanent_infeasibility_and_explicit_cutoff(self):
        data = single_process([order("O", 17)])
        run = simulate(data, Snapshot(MONDAY))
        self.assertEqual(run.experiment.iloc[0].status, "infeasible")
        self.assertEqual(run.diagnostics.iloc[0].code, "operation_infeasible")
        short = single_process([order("O", 1)])
        limited = simulate(short, Snapshot(MONDAY), limits=RunLimits(MONDAY + pd.Timedelta(minutes=1)))
        self.assertEqual(limited.experiment.iloc[0].status, "limit_reached")
        self.assertEqual(limited.final["J"].iloc[0].state, "processing")

    def test_workbook_round_trip(self):
        data, initial, rfq = example_factory()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "example.xlsx"
            with pd.ExcelWriter(path, engine="openpyxl") as writer:
                for name in TABLES:
                    getattr(data, name).to_excel(writer, sheet_name=name, index=False)
                initial.wip.to_excel(writer, sheet_name="WIP", index=False)
                initial.inventory.to_excel(writer, sheet_name="Inventory", index=False)
                initial.pending_deliveries.to_excel(writer, sheet_name="PendingDeliveries", index=False)
            loaded, stock = load_workbook(path, start=initial.start)
            actual = simulate(loaded, stock, rfq=rfq)
            expected = simulate(data, initial, rfq=rfq)
            pd.testing.assert_frame_equal(actual.orders, expected.orders)

    def test_rfq_when_the_baseline_has_no_orders(self):
        data = single_process([])
        rfq = frame("Orders", [order("RFQ", 1)])
        comparison = compare_rfq(data, Snapshot(MONDAY), rfq)
        self.assertTrue(comparison.existing_orders.empty)
        self.assertEqual(comparison.accepted.orders.iloc[0].completion_time, MONDAY + pd.Timedelta(minutes=30))


class InventoryTests(unittest.TestCase):
    def test_placement_clock_and_overlapping_deliveries_between_shifts(self):
        material = frame("Materials", [("M", "metal", ["vendor"], 2, 90, 60, None)])
        data = single_process([order("O", 6)], duration=10, parts=[("p", ["proc"], {0: {"M": 1}})], materials=material)
        stock = pd.DataFrame([{"ID_mat": "M", "cq": 2, "next_order_time": "2026-11-16 05:00"}])
        run = simulate(data, Snapshot("2026-11-16 05:00", inventory=stock))
        placements = events(run, "material_order")
        delivered = events(run, "material_delivery")
        self.assertEqual(placements.timestamp.tolist()[:3], [pd.Timestamp(f"2026-11-16 0{i}:00") for i in (5, 6, 7)])
        self.assertEqual(delivered.timestamp.tolist(), [pd.Timestamp("2026-11-16 06:30"),
                                                       pd.Timestamp("2026-11-16 07:30"),
                                                       pd.Timestamp("2026-11-16 08:30")])
        self.assertEqual(events(run, "material_consumption").iloc[0].timestamp, MONDAY)
        self.assertEqual(run.final["Inv"].iloc[0].cq, 2)
        self.assertGreater(len(run.final["Inv"].iloc[0].pending_deliveries), 0)

    def test_shortage_blocks_or_waits_and_material_is_part_specific(self):
        material = frame("Materials", [("M", "metal", ["seller"], 2, 90, 60, None)])
        data = single_process([order("hungry", 1), order("free", 1, part="free")],
                              parts=[("p", ["proc"], {0: {"M": 1}}), ("free", ["proc"], {})], materials=material)
        stock = pd.DataFrame([{"ID_mat": "M", "cq": 0}])
        blocked = simulate(data, Snapshot(MONDAY, inventory=stock))
        self.assertEqual(events(blocked, "operation_start").entity_id.tolist(), [("free", 1)])
        self.assertEqual(blocked.diagnostics.iloc[0].code, "material_unavailable")
        arrival = pd.DataFrame([{"ID_PO": "P", "ID_mat": "M", "q": 1, "arrival": MONDAY + pd.Timedelta(hours=1)}])
        supplied = simulate(data, Snapshot(MONDAY, inventory=stock, pending_deliveries=arrival))
        start = events(supplied, "material_consumption").iloc[0]
        self.assertEqual(start.timestamp, MONDAY + pd.Timedelta(hours=1))
        self.assertEqual(start.related_entity_id, ("hungry", 1))


class EfficiencyTests(unittest.TestCase):
    def test_probability_and_caps(self):
        self.assertEqual(downtime_probability(2, 1), 1)
        self.assertEqual(downtime_probability(1, 2), 0.5)
        self.assertEqual(downtime_probability(0, 1), 0)
        data = single_process([order("O", 1)], duration=1)
        settings = EfficiencySettings(.5, pareto_minimum=5, sample_duration=lambda rng: 1000,
                                      initial_remaining={"S": 2})
        near_end = MONDAY + pd.Timedelta(hours=7, minutes=58)
        data.Orders.at[0, "RD"] = near_end
        result = simulate(data, Snapshot(near_end), efficiency=settings, seed=4)
        first = result.station_shifts.iloc[0]
        self.assertEqual(first.generic_unavailability_time, 2)
        self.assertEqual(first.loss_budget_remaining, 0)
        self.assertEqual(first.processing_time, 0)

    def test_no_efficient_or_idle_downtime_and_reproducibility(self):
        data = single_process([order("O", 1, MONDAY + pd.Timedelta(minutes=30))])
        ideal = simulate(data, Snapshot(MONDAY), efficiency=EfficiencySettings(1))
        self.assertEqual(ideal.station_shifts.iloc[0].generic_unavailability_time, 0)
        self.assertTrue(ideal.sampled_inputs.empty)
        settings = EfficiencySettings(.90)
        first = simulate(data, Snapshot(MONDAY), efficiency=settings, seed=7)
        repeat = simulate(data, Snapshot(MONDAY), efficiency=settings, seed=7)
        pd.testing.assert_frame_equal(first.event_log, repeat.event_log)
        pd.testing.assert_frame_equal(first.sampled_inputs, repeat.sampled_inputs)
        self.assertTrue((first.sampled_inputs.timestamp >= MONDAY + pd.Timedelta(minutes=30)).all())
        self.assertTrue((first.station_shifts.generic_unavailability_time <= first.station_shifts.loss_budget_initial).all())

    def test_planner_input_and_stratum_mass(self):
        data = single_process([order("O", 1)])
        with self.assertRaisesRegex(ValueError, "sum to one"):
            monte_carlo(data, Snapshot(MONDAY), runs=2, planner_weights={.9: .7})
        point = monte_carlo(data, Snapshot(MONDAY), runs=2, efficiency_levels=1)
        self.assertTrue(point.planner_weights.empty)
        self.assertTrue(point.weighted_delivery.empty)
        beliefs = pd.DataFrame({"efficiency_level": [.9, 1.0], "probability_weight": [.25, .75]})
        weighted = monte_carlo(data, Snapshot(MONDAY), runs={.9: 2, 1.0: 3}, planner_weights=beliefs)
        mass = weighted.weighted_delivery.groupby("efficiency_level").probability_mass.sum()
        self.assertEqual(list(mass), [.25, .75])
        self.assertTrue(weighted.experiments.run_id.is_unique)

    def test_percentiles_combine_underlying_samples_and_retain_censored_mass(self):
        samples = pd.DataFrame({"scenario_id": ["s"] * 3, "ID_O": ["O"] * 3,
                                "completion_time": [MONDAY, MONDAY + pd.Timedelta(days=10), pd.NaT],
                                "due_date": MONDAY + pd.Timedelta(days=2), "end_time": MONDAY,
                                "probability_mass": [.85, .06, .09]})
        row = summarize_delivery(samples, ["scenario_id", "ID_O"]).iloc[0]
        self.assertEqual(row.p90, MONDAY + pd.Timedelta(days=10))
        self.assertTrue(pd.isna(row.p95))
        self.assertTrue(pd.isna(row.mean_completion_time))
        self.assertAlmostEqual(row.completion_probability, .91)

    def test_generator_samples_ordinary_pareto_and_then_caps(self):
        data = single_process([order("O", 10)], duration=35)
        settings = EfficiencySettings(.5)
        result = simulate(data, Snapshot(MONDAY), efficiency=settings, seed=5)
        station_branch = np.random.SeedSequence(5).spawn(2)[0]
        rng = np.random.default_rng(station_branch.spawn(1)[0])
        self.assertLess(rng.random(), .5)
        sample = 5 * (1 + rng.pareto(1.161))
        logged = result.sampled_inputs.query("parameter == 'generic_duration_minutes'").iloc[0]
        self.assertEqual(logged.sampled_value, sample)
        self.assertLessEqual(logged.details["applied_minutes"], 240)

    def test_zero_efficiency_terminates_with_capacity_diagnostic(self):
        data = single_process([order("O", 1)])
        result = simulate(data, Snapshot(MONDAY), efficiency=EfficiencySettings(0))
        self.assertEqual(result.experiment.iloc[0].status, "blocked")
        self.assertEqual(result.diagnostics.iloc[0].code, "zero_productive_capacity")

    def test_saturation_experiment_reports_capacity_and_delivery(self):
        capacity, delivery = validate_efficiency(runs=2, seed=42, cases=[Case("test")], levels=[.75, 1.0])
        self.assertTrue((capacity.realized_loss <= capacity.target_loss).all())
        self.assertEqual(capacity.loc[capacity.efficiency == 1, "realized_loss"].item(), 0)
        self.assertEqual(delivery.ID_O.tolist(), ["target", "target"])


class RiskTests(unittest.TestCase):
    def test_operating_clock_pauses_in_idle_and_defers_failure(self):
        data = single_process([order("A", 1, MONDAY + pd.Timedelta(minutes=30), MONDAY),
                               order("B", 1, MONDAY + pd.Timedelta(minutes=60))], duration=20, setup=10)
        data.Workstations["previous_part"] = ["p"]
        risk = RiskSettings(MONDAY + pd.Timedelta(hours=3), {"S": MachineReliability(100, 15, 5)})
        result = simulate(data, Snapshot(MONDAY), risk=risk)
        outage = events(result, "unavailability_start").iloc[0]
        self.assertEqual(outage.details["due_at"], MONDAY + pd.Timedelta(minutes=35))
        self.assertEqual(outage.timestamp, MONDAY + pd.Timedelta(minutes=60))
        self.assertEqual(events(result, "station_recovery").iloc[0].timestamp, MONDAY + pd.Timedelta(minutes=75))
        self.assertEqual(result.orders.completion_time.tolist(), [MONDAY + pd.Timedelta(minutes=60),
                                                                  MONDAY + pd.Timedelta(minutes=105)])
        self.assertEqual(result.stations.iloc[0].machine_downtime, 15)
        self.assertFalse((result.sampled_inputs.parameter == "generic_trigger").any())

    def test_supplier_draws_fixed_observation_tail_and_reproducibility(self):
        material = frame("Materials", [("M", "metal", ["vendor"], 1, Parameter(45, lambda rng: rng.integers(30, 90)),
                                        Parameter(90, lambda rng: rng.integers(80, 95)), None)])
        data = single_process([order("O", 1)], materials=material, parts=[("p", ["proc"], {0: {"M": 1}})])
        stock = pd.DataFrame([{"ID_mat": "M", "cq": 3, "next_order_time": MONDAY}])
        initial = Snapshot(MONDAY, inventory=stock)
        settings = RiskSettings(MONDAY + pd.Timedelta(hours=4))
        result = simulate(data, initial, risk=settings, seed=20, inventory=InventoryRules(sample_otp=True))
        repeat = simulate(data, initial, risk=settings, seed=20, inventory=InventoryRules(sample_otp=True))
        pd.testing.assert_frame_equal(result.sampled_inputs, repeat.sampled_inputs)
        samples = result.sampled_inputs
        self.assertTrue((samples.parameter == "order_interval_minutes").any())
        self.assertTrue(any(d.get("after_production") for d in samples.details))
        self.assertEqual(result.final["Inv"].iloc[0].cq, 2)
        baseline = simulate(data, initial)
        self.assertTrue(baseline.sampled_inputs.empty)
        self.assertEqual(events(baseline, "material_order").iloc[0].details["lead_time_minutes"], 45)

    def test_sensitivity_uses_sampled_operating_exposure(self):
        data = single_process([order("A", 1, due=MONDAY), order("B", 1)], duration=20)
        risk = RiskSettings(MONDAY + pd.Timedelta(hours=2),
                            {"S": MachineReliability(200, lambda rng: rng.integers(1, 100), 5)})
        experiment = monte_carlo(data, Snapshot(MONDAY), risk=risk, runs=9, seed=42)
        analysis = analyze_risk(experiment, "B", scenario_id="baseline")
        row = analysis.correlations.loc[analysis.correlations.metric == "reference_repair_minutes"].iloc[0]
        self.assertAlmostEqual(row.pearson, 1)
        self.assertAlmostEqual(row.spearman, 1)
        self.assertAlmostEqual(analysis.diagnostics.iloc[0].r_squared, 1)
        self.assertEqual(analysis.diagnostics.iloc[0].excluded_unfinished, 0)
        realized = analyze_risk(experiment, "B", scenario_id="baseline", machine_exposure="realized",
                                outcome="waiting_minutes")
        self.assertIn("waiting_minutes", realized.observations.columns)
        self.assertEqual(realized.correlations.metric.tolist(), ["realized_failure_count", "realized_repair_minutes"])

    def test_partial_operation_keeps_actual_time_to_failure(self):
        data = single_process([order("O", 1)], duration=30)
        risk = RiskSettings(MONDAY + pd.Timedelta(hours=2), {"S": MachineReliability(200, 10, 5)})
        result = simulate(data, Snapshot(MONDAY), risk=risk, limits=RunLimits(MONDAY + pd.Timedelta(minutes=3)))
        self.assertEqual(result.final["S"].iloc[0].time_to_failure_operating, pd.Timedelta(minutes=2))
        self.assertTrue(events(result, "unavailability_start").empty)


if __name__ == "__main__":
    unittest.main()
