"""The discrete-event engine shared by all FACTS execution modes."""

from dataclasses import dataclass

import pandas as pd

from .calendar import FactoryState, timestamp
from .events import Event, EventQueue
from .inventory import InventoryRules, Purchasing, StockShortage
from .model import Model, minute_count
from .results import Journal, RunResult, snapshot
from .scheduling import Scheduler
from .uncertainty import Uncertainty


TIME_FIELDS = ("observed_shift_time", "processing_time", "generic_unavailability_time", "machine_downtime",
               "initial_downtime", "idle_no_work", "idle_shift_constraint", "idle_material", "idle_dispatch")


@dataclass(frozen=True)
class RunLimits:
    until: object = None
    max_events: int = 20_000


def simulate(data, initial, *, rfq=None, efficiency=None, risk=None, seed=0, inventory=None, limits=None,
             scenario_id=None, run_id=1, scheduling_policy=None, mode=None):
    """Run one scenario. All durations in the input tables are in minutes."""
    if efficiency is not None and risk is not None:
        raise ValueError("Efficiency and explicit operational risk require separate runs")
    selected = "efficiency_mc" if efficiency is not None else "operational_risk_mc" if risk is not None else "deterministic"
    if mode is not None and mode != selected:
        raise ValueError(f"Mode {mode!r} does not match the supplied settings for {selected!r}")
    if scenario_id is None:
        scenario_id = "baseline" if rfq is None else "rfq"
    engine = Simulation(Model(data, initial, rfq), inventory or InventoryRules(), limits or RunLimits(),
                        efficiency, risk, seed, scenario_id, run_id, scheduling_policy, selected)
    rfq_ids = [] if rfq is None else list(rfq["ID_O"])
    return engine.run(rfq_ids)


class Simulation:
    def __init__(self, model, inventory, limits, efficiency, risk, seed, scenario, run_id, rule, mode):
        self.model = model
        self.now = model.start
        self.queue = EventQueue()
        self.journal = Journal(scenario, run_id)
        self.factory = FactoryState(model.calendar.next_slot(self.now))
        self.purchasing = Purchasing(inventory)
        self.scheduler = Scheduler(rule or model.factory["scheduling_policy"], model.factory["priority_rules"],
                                   inventory.material_at_dispatch)
        self.uncertainty = Uncertainty(model, seed, efficiency, risk)
        self.limits = limits
        self.until = timestamp(limits.until) if limits.until is not None else None
        if self.until is not None and self.until < self.now:
            raise ValueError("Run limit precedes the snapshot")
        if not isinstance(limits.max_events, int) or limits.max_events <= 0:
            raise ValueError("Run limit max_events must be a positive integer")
        self.configuration = {"mode": mode, "seed": seed, "efficiency": efficiency, "risk": risk,
                              "inventory_rules": inventory, "limits": limits, "scheduling_policy": self.scheduler.rule,
                              "priority_rules": dict(self.scheduler.priorities)}
        self.wait = dict.fromkeys(model.jobs, 0.0)
        self.station_shifts = []
        self.shift_rows = {}
        self.station_counts = dict.fromkeys(model.stations, 0)
        self.elapsed_machine_downtime = dict.fromkeys(model.stations, 0.0)
        self.idle = {}
        self.diagnostics = []
        self.status = "complete"
        self.initial = None

    def _initialize(self):
        shift = self.factory.shift
        if shift is not None and shift.t_start < self.now:
            self._start_shift(partial=True)
        elif shift is not None:
            self.queue.add(shift.t_start, Event.SHIFT_START)
        for job in self.model.jobs.values():
            if job.state == "processing":
                self.queue.add(job.t_done, Event.OPERATION_DONE, job.ID_J)
        for order in self.model.orders.values():
            if any(j.state == "unreleased" for j in self.model.jobs_by_order[order.ID_O]):
                self.queue.add(order.RD, Event.ORDER_RELEASE, order.ID_O)
        for station in self.model.stations.values():
            if station.state == "unavailable":
                self.queue.add(station.t_done, Event.STATION_RECOVERY, station.ID_WS)
        self.purchasing.initialize(self)
        self.uncertainty.initialize(self)
        self.initial = snapshot(self)

    def run(self, rfq_ids):
        self._initialize()
        handled = 0
        while True:
            while self.queue.next_time == self.now:
                if handled >= self.limits.max_events:
                    self._limit("event_limit")
                    return self._results(rfq_ids)
                _, event, payload = self.queue.pop()
                self._handle(event, payload)
                handled += 1
            if all(j.state == "finished" for j in self.model.jobs.values()):
                break
            try:
                if self.factory.active:
                    self._dispatch()
                    if self.queue.next_time == self.now:
                        continue
                if not self._can_advance():
                    break
            except StockShortage as error:
                self.status = "blocked"
                self.diagnostics.append((error.job_id, "stock_shortage", str(error)))
                break
            next_time = self.queue.next_time
            if self.until is not None and next_time > self.until:
                self._advance(self.until)
                self._limit("time_limit")
                break
            self._advance(next_time)
        return self._results(rfq_ids)

    def _limit(self, code):
        self.status = "limit_reached"
        self.diagnostics.append((None, code, "Run limit reached with work unfinished"))

    def _handle(self, event, payload):
        if event == Event.OPERATION_DONE:
            self._finish_operation(self.model.jobs[payload])
        elif event == Event.STATION_RECOVERY:
            station = self.model.stations[payload]
            self.journal.event(self.now, "station", payload, "station_recovery", station.state, "available",
                               reason=station.reason)
            station.state, station.t_done, station.reason = "available", None, None
            self.uncertainty.recover(self, station)
        elif event == Event.SHIFT_END:
            self._end_shift()
        elif event == Event.SHIFT_START:
            self._start_shift(partial=False)
        elif event == Event.ORDER_RELEASE:
            self.journal.event(self.now, "order", payload, "order_release", "unreleased", "released")
            for job in self.model.jobs_by_order[payload]:
                if job.state == "unreleased":
                    job.state, job.ready_at = "waiting", self.now
        elif event == Event.MATERIAL_ORDER:
            self.purchasing.place(self, payload)
        elif event == Event.MATERIAL_DELIVERY:
            self.purchasing.deliver(self, *payload)
        elif event == Event.MACHINE_FAILURE:
            self.uncertainty.failure(self, *payload)

    def _start_shift(self, *, partial):
        shift = self.factory.shift
        self.factory.active = True
        target = self.uncertainty.begin_shift(self, partial=partial)
        for key in self.model.stations:
            remaining = self.uncertainty.budget[key]
            row = {"ID_WS": key, "Shift_ID": shift.Shift_ID, "shift_number": shift.shift_number,
                   "shift_start": shift.t_start, "shift_end": shift.t_done,
                   "efficiency_assumed": self.uncertainty.level,
                   "scheduled_shift_time": minute_count(shift.t_done - shift.t_start),
                   "loss_budget_initial": minute_count(target),
                   "loss_budget_at_snapshot": None if remaining is None else minute_count(remaining),
                   "loss_budget_remaining": None if remaining is None else minute_count(remaining),
                   "completed_operations": 0, **dict.fromkeys(TIME_FIELDS, 0.0)}
            self.shift_rows[key] = row
            self.station_shifts.append(row)
        self.journal.event(self.now, "factory", shift.Shift_ID, "shift_initialized" if partial else "shift_start",
                           False, True, shift_number=shift.shift_number)
        self.queue.add(shift.t_done, Event.SHIFT_END)

    def _end_shift(self):
        shift = self.factory.shift
        self.journal.event(self.now, "factory", shift.Shift_ID, "shift_end", True, False,
                           shift_number=shift.shift_number)
        self.factory.active = False
        self.shift_rows.clear()
        self.factory.shift = self.model.calendar.next_slot(self.now)
        if self.factory.shift is not None:
            self.queue.add(self.factory.t_start, Event.SHIFT_START)

    def _dispatch(self):
        model = self.model
        remaining = self.factory.t_done - self.now
        while True:
            options = self.scheduler.feasible(
                model.jobs, model.stations, model.inventory, model.parts, model.processes, remaining,
                shortage=lambda job, required: self.purchasing.shortage(self, job, required))
            assignments = self.scheduler.schedule(options, model.jobs, model.stations, model.inventory)
            lost_capacity = False
            for assignment in assignments:
                key = assignment.ID_WS
                duration = self.uncertainty.generic_duration(self, key)
                if duration is not None:
                    self.start_unavailability(model.stations[key], duration, "GENERIC_EFFICIENCY")
                    self.shift_rows[key]["loss_budget_remaining"] = minute_count(self.uncertainty.budget[key])
                    lost_capacity = True
            if not lost_capacity:
                break
        for assignment in assignments:
            self._start_operation(assignment)
        self.idle = {}
        for station in model.stations.values():
            if station.state == "available":
                self.idle[station.ID_WS] = self._idle_category(station, remaining)

    def _idle_category(self, station, remaining):
        model = self.model
        categories = set()
        for job in model.jobs.values():
            if job.state != "waiting":
                continue
            op = self.scheduler.operation(job, station, model.parts, model.processes)
            if op is None:
                continue
            bundle = self.scheduler.requirements(job, model.parts[job.ID_pt])
            if not self.scheduler.has_stock(bundle, model.inventory):
                categories.add("idle_material")
            elif op[1] <= remaining:
                return "idle_dispatch"
            else:
                categories.add("idle_shift_constraint")
        return next((label for label in ("idle_shift_constraint", "idle_material") if label in categories), "idle_no_work")

    def _start_operation(self, assignment):
        job = self.model.jobs[assignment.ID_J]
        station = self.model.stations[assignment.ID_WS]
        for key, quantity in assignment.material.items():
            stock = self.model.inventory[key]
            before = stock.cq
            stock.consume(quantity)
            self.journal.event(self.now, "inventory", key, "material_consumption", before, stock.cq,
                               job.ID_J, q=quantity, step=job.step)
        completion = self.now + assignment.duration
        job.state, job.ID_WS, job.t_done = "processing", station.ID_WS, completion
        station.state, station.ID_J, station.t_done = "processing", job.ID_J, completion
        self.queue.add(completion, Event.OPERATION_DONE, job.ID_J)
        self.uncertainty.run_operation(self, station.ID_WS, completion)
        self.journal.event(self.now, "job", job.ID_J, "operation_start", "waiting", "processing", station.ID_WS,
                           step=job.step, process=assignment.ID_proc, t_done=completion,
                           setup_minutes=assignment.setup_minutes, setup_source=assignment.setup_source)
        self.journal.event(self.now, "station", station.ID_WS, "station_assignment", "available", "processing",
                           job.ID_J, t_done=completion)

    def _finish_operation(self, job):
        station = self.model.stations[job.ID_WS]
        step = job.step
        if step == len(self.model.parts[job.ID_pt].routing) - 1:
            job.state, job.completed_at = "finished", self.now
        else:
            job.state, job.step, job.ready_at = "waiting", step + 1, self.now
        job.ID_WS, job.t_done = None, None
        station.state, station.ID_J, station.t_done = "available", None, None
        station.previous_part = job.ID_pt
        self.station_counts[station.ID_WS] += 1
        if station.ID_WS in self.shift_rows:
            self.shift_rows[station.ID_WS]["completed_operations"] += 1
        self.journal.event(self.now, "job", job.ID_J, "operation_complete", "processing", job.state, station.ID_WS,
                           step=step, next_step=job.step if job.state == "waiting" else None)
        self.journal.event(self.now, "station", station.ID_WS, "station_release", "processing", "available", job.ID_J)

    def start_unavailability(self, station, duration, reason, **details):
        if station.state != "available":
            raise RuntimeError("Unavailability requires an available station")
        station.state, station.reason, station.t_done = "unavailable", reason, self.now + duration
        self.queue.add(station.t_done, Event.STATION_RECOVERY, station.ID_WS)
        self.journal.event(self.now, "station", station.ID_WS, "unavailability_start", "available", station.state,
                           reason=reason, duration_minutes=minute_count(duration), t_done=station.t_done, **details)

    def _advance(self, when):
        if when < self.now:
            raise RuntimeError("The DES clock cannot move backward")
        elapsed = minute_count(when - self.now)
        for job in self.model.jobs.values():
            if job.state == "waiting":
                self.wait[job.ID_J] += elapsed
        for station in self.model.stations.values():
            if station.state == "unavailable" and station.reason == "MACHINE_FAILURE":
                self.elapsed_machine_downtime[station.ID_WS] += elapsed
            elif station.state == "processing":
                self.uncertainty.advance_operation(station.ID_WS, when - self.now)
        for key, row in self.shift_rows.items():
            station = self.model.stations[key]
            if station.state == "processing":
                field = "processing_time"
            elif station.state == "unavailable":
                field = {"GENERIC_EFFICIENCY": "generic_unavailability_time", "MACHINE_FAILURE": "machine_downtime"}
                field = field.get(station.reason, "initial_downtime")
            else:
                field = self.idle.get(key, "idle_no_work")
            row[field] += elapsed
            row["observed_shift_time"] += elapsed
        self.now = when

    def _can_advance(self):
        jobs = self.model.jobs.values()
        if self.queue.next_time is not None and any(j.state in ("processing", "unreleased") for j in jobs):
            return True
        blockers = []
        for job in jobs:
            if job.state == "finished":
                continue
            blocker = self._blocker(job)
            if blocker is None and self.queue.next_time is not None:
                return True
            blockers.append((job.ID_J, *(blocker or ("no_events", "No pending events can advance the job"))))
        self.status = "infeasible" if any(code == "operation_infeasible" for _, code, _ in blockers) else "blocked"
        self.diagnostics.extend(blockers)
        return False

    def _blocker(self, job):
        model = self.model
        part = model.parts[job.ID_pt]
        process = model.processes[part.routing[job.step]]
        nominal = pd.Timedelta(minutes=process.unit_time(job.ID_pt) * float(job.q))
        compatible = [s for s in model.stations.values() if s.group in process.groups]
        if not compatible or model.calendar.next_slot(self.now, nominal) is None:
            return "operation_infeasible", "No compatible station or calendar shift fits the nominal operation"
        for station in compatible:
            _, duration, _, _ = self.scheduler.operation(job, station, model.parts, model.processes)
            available = max(self.now, station.t_done) if station.t_done is not None else self.now
            if model.calendar.next_slot(available, duration) is not None:
                break
        else:
            return "operation_infeasible", "No applicable shift fits the operation with the current setups"
        requirements = self.scheduler.requirements(job, part)
        if not self.scheduler.has_stock(requirements, model.inventory):
            self.purchasing.shortage(self, job, requirements)
            for key, amount in requirements.items():
                inventory = model.inventory[key]
                expected = inventory.cq + sum(d.q for d in inventory.pending_deliveries.values())
                if expected < amount and inventory.next_order_time is None:
                    return "material_unavailable", f"Insufficient stock or pending supply for {key!r}"
        efficiency = self.uncertainty.efficiency
        if efficiency is not None and efficiency.level == 0 and efficiency.short_budget == "cap":
            active = self.factory.active
            partial_capacity = active and any(budget is None or budget < self.factory.t_done - self.now
                                              for budget in self.uncertainty.budget.values())
            if not partial_capacity:
                return "zero_productive_capacity", "Zero efficiency exhausts all available time in each full shift"
        return None

    def _results(self, rfq_ids):
        j = self.journal
        jobs = j.table([(job.ID_J, job.ID_O, job.ID_pt, job.state, job.completed_at, self.wait[job.ID_J])
                        for job in self.model.jobs.values()],
                       ["ID_J", "ID_O", "ID_pt", "state", "completion_time", "waiting_minutes"])
        order_rows = []
        rfq_times = {}
        for order in self.model.orders.values():
            lines = self.model.jobs_by_order[order.ID_O]
            finished = all(job.completed_at is not None for job in lines)
            completion = max(job.completed_at for job in lines) if finished else None
            if order.ID_O in rfq_ids:
                rfq_times[order.ID_O] = completion
            lead = completion - order.RD if finished else None
            late = completion - order.DD if finished else None
            tardy = max(pd.Timedelta(0), late) if finished else None
            order_rows.append((order.ID_O, order.RD, order.DD, completion, lead, late, tardy, finished,
                               sum(self.wait[job.ID_J] for job in lines)))
        orders = j.table(order_rows, ["ID_O", "release_time", "due_date", "completion_time", "lead_time", "lateness",
                                      "tardiness", "completed", "waiting_minutes"])
        shift_columns = ["ID_WS", "Shift_ID", "shift_number", "shift_start", "shift_end", "efficiency_assumed",
                         "scheduled_shift_time", "loss_budget_initial", "loss_budget_at_snapshot",
                         "loss_budget_remaining", "completed_operations", *TIME_FIELDS]
        shifts = j.table([tuple(row[c] for c in shift_columns) for row in self.station_shifts], shift_columns)
        stations = []
        for key in self.model.stations:
            station_shifts = shifts.loc[shifts["ID_WS"].map(lambda value: value == key).astype(bool)]
            observed = station_shifts["observed_shift_time"].sum()
            processing = station_shifts["processing_time"].sum()
            stations.append((key, self.station_counts[key], station_shifts["scheduled_shift_time"].sum(),
                             *[station_shifts[field].sum() for field in TIME_FIELDS],
                             self.elapsed_machine_downtime[key], processing / observed if observed else None))
        stations = j.table(stations, ["ID_WS", "completed_operations", "scheduled_shift_time", *TIME_FIELDS,
                                        "machine_downtime_elapsed", "utilization"])
        metrics = {key: stations[key].sum() for key in TIME_FIELDS}
        metrics.update(completed_orders=sum(row[7] for row in order_rows), waiting_minutes=sum(self.wait.values()),
                       machine_downtime_elapsed=stations["machine_downtime_elapsed"].sum())
        rfq_completion = max(rfq_times.values()) if rfq_times and all(t is not None for t in rfq_times.values()) else None
        experiment = j.table([(self.configuration["mode"], self.configuration["seed"], self.status, self.now,
                               self.uncertainty.evaluation_level, rfq_completion, rfq_times, metrics)],
                             ["mode", "seed", "status", "end_time", "efficiency_level", "rfq_completion_time",
                              "rfq_completion_times", "factory_metrics"])
        final = snapshot(self)
        self._observe_risk_supply()
        return RunResult(self.initial, final, pd.DataFrame(j.events, columns=j.event_columns),
                         pd.DataFrame(j.draws, columns=j.draw_columns), jobs, orders, shifts, stations, experiment,
                         j.table(self.diagnostics, ["ID_J", "code", "message"]), self.configuration)

    def _observe_risk_supply(self):
        """Continue exogenous purchasing to the fixed exposure cutoff after production ends."""
        cutoff = self.uncertainty.exposure_end
        if cutoff is None or not self.model.inventory or cutoff <= self.now:
            return
        draws_before = len(self.journal.draws)
        events_before = len(self.journal.events)
        original = self.queue
        self.queue = EventQueue()
        while original.next_time is not None and original.next_time < cutoff:
            when, event, payload = original.pop()
            if event in (Event.MATERIAL_ORDER, Event.MATERIAL_DELIVERY):
                self.queue.add(when, event, payload)
        handled = 0
        while self.queue.next_time is not None and self.queue.next_time < cutoff:
            if handled >= self.limits.max_events:
                raise ValueError("Purchasing exceeded the event limit in the risk observation window")
            self.now, event, payload = self.queue.pop()
            self._handle(event, payload)
            handled += 1
        for row in self.journal.draws[draws_before:]:
            row[-1]["after_production"] = True
        for row in self.journal.events[events_before:]:
            row[-1]["after_production"] = True
