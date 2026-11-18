"""Factory data, snapshot records, and dynamic simulation entities."""

import ast
from copy import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from math import isfinite
from numbers import Integral, Real
from typing import Hashable

import numpy as np
import pandas as pd

from .calendar import ShiftCalendar, timestamp


TABLES = {
    "FACTS": ("factory_ID", "factory_name", "industry", "n_employees", "shift_schedule",
              "shift_exceptions", "scheduling_policy", "priority_rules"),
    "Parts": ("ID_pt", "routing", "BOM"),
    "Processes": ("ID_proc", "tp", "tsu", "WS_groups"),
    "Workstations": ("ID_WS", "WS_group", "availability", "t_down"),
    "Orders": ("ID_O", "lines", "RD", "DD", "status"),
    "Materials": ("ID_mat", "family", "suppliers", "q", "DLT", "OTP", "last_order"),
}


def missing(value):
    return pd.api.types.is_scalar(value) and bool(pd.isna(value))


def number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{name} must be {qualifier}, got {value!r}")
    return float(value)


def minutes(value, name):
    return pd.Timedelta(minutes=number(value, name))


def quantity(value, name, *, positive=False):
    if isinstance(value, Decimal):
        if not value.is_finite() or value < 0 or (positive and value == 0):
            raise ValueError(f"Invalid quantity for {name}: {value!r}")
        return value
    number(value, name, positive=positive)
    return Decimal(str(value))


def minute_count(duration):
    return duration / pd.Timedelta(minutes=1)


def records(frame, table, required):
    if not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique:
        raise ValueError(f"{table} must be a DataFrame with unique column names")
    absent = set(required) - set(frame.columns)
    if absent:
        raise ValueError(f"{table} is missing columns {sorted(absent)}")
    return frame.to_dict("records")


def indexed(frame, table, required, key):
    result = {}
    for row in records(frame, table, required):
        identifier = row[key]
        if missing(identifier) or not isinstance(identifier, Hashable) or identifier == "":
            raise ValueError(f"Invalid {key} in {table}: {identifier!r}")
        if identifier in result:
            raise ValueError(f"Duplicate {key} in {table}: {identifier!r}")
        result[identifier] = row
    return result


def sequence(value, name, *, nonempty=False):
    if not isinstance(value, (list, tuple)) or (nonempty and not value):
        raise ValueError(f"{name} must be {'a nonempty' if nonempty else 'a'} list or tuple")
    return tuple(value)


def quantities(value, name):
    entries = value.items() if isinstance(value, Mapping) else sequence(value, name)
    result = {}
    for key, amount in entries:
        if key in result:
            raise ValueError(f"Duplicate material {key!r} in {name}")
        result[key] = quantity(amount, name, positive=True)
    return result


@dataclass(frozen=True)
class Parameter:
    """Explicit deterministic value and optional configured NumPy sampler."""

    nominal: float
    sampler: Callable[[np.random.Generator], float] | None = None

    def value(self, rng=None):
        return self.sampler(rng) if rng is not None and self.sampler is not None else self.nominal


@dataclass(frozen=True)
class FactoryData:
    FACTS: pd.DataFrame
    Parts: pd.DataFrame
    Processes: pd.DataFrame
    Workstations: pd.DataFrame
    Orders: pd.DataFrame
    Materials: pd.DataFrame


@dataclass(frozen=True)
class Snapshot:
    start: object
    wip: pd.DataFrame | None = None
    inventory: pd.DataFrame | None = None
    pending_deliveries: pd.DataFrame | None = None


@dataclass(frozen=True)
class Part:
    routing: tuple
    BOM: Mapping

    def materials(self, step):
        return self.BOM.get(step, {})


@dataclass(frozen=True)
class Process:
    tp: object
    tsu: object
    groups: tuple

    def unit_time(self, part_id):
        value = self.tp
        if isinstance(value, Mapping):
            if part_id not in value:
                raise ValueError(f"Missing processing time for part {part_id!r}")
            value = value[part_id]
        return number(value, f"Processing time for part {part_id!r}")

    def setup(self, previous, current):
        if missing(previous):
            return 0.0, "unknown_previous_setup"
        value = self.tsu.get((previous, current)) if isinstance(self.tsu, Mapping) else self.tsu
        if missing(value):
            return 0.0, "missing_assumed_zero"
        value = number(value, f"Setup time for {previous!r} to {current!r}")
        return value, "explicit_zero" if value == 0 else "measured"


@dataclass(frozen=True)
class Order:
    ID_O: Hashable
    lines: tuple
    RD: pd.Timestamp
    DD: pd.Timestamp


@dataclass
class Job:
    order: Order
    line: int
    ID_pt: Hashable
    q: Decimal
    state: str = "unreleased"
    step: int = 0
    t_done: pd.Timestamp | None = None
    ID_WS: Hashable | None = None
    ready_at: pd.Timestamp | None = None
    completed_at: pd.Timestamp | None = None

    @property
    def ID_J(self):
        return self.order.ID_O, self.line

    @property
    def ID_O(self):
        return self.order.ID_O


@dataclass
class Station:
    ID_WS: Hashable
    group: Hashable
    state: str
    previous_part: Hashable | None
    t_done: pd.Timestamp | None = None
    ID_J: tuple | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Delivery:
    ID_PO: Hashable
    q: Decimal
    arrival: pd.Timestamp
    supplier: Hashable | None = None
    placed_at: pd.Timestamp | None = None
    nominal_dlt: float | None = None


@dataclass
class Inventory:
    ID_mat: Hashable
    cq: Decimal
    next_order_time: pd.Timestamp | None = None
    pending_deliveries: dict = field(default_factory=dict)

    def consume(self, quantity):
        if quantity > self.cq:
            raise ValueError(f"Insufficient stock for material {self.ID_mat!r}")
        self.cq -= quantity

    def receive(self, purchase_id):
        delivery = self.pending_deliveries.pop(purchase_id)
        self.cq += delivery.q
        return delivery


class Model:
    def __init__(self, data, snapshot, rfq):
        factories = records(data.FACTS, "FACTS", TABLES["FACTS"])
        if len(factories) != 1:
            raise ValueError("FACTS must contain exactly one factory row")
        factory = factories[0]
        self.factory = factory
        self.start = timestamp(snapshot.start)
        self.calendar = ShiftCalendar(factory["shift_schedule"], factory["shift_exceptions"])
        self.materials = self._materials(data)
        self.parts = self._parts(data)
        self.processes = self._processes(data)
        self.station_data = self._table(data, "Workstations")
        self.stations = self._stations()
        self.orders = self._orders(data.Orders, rfq)
        self.jobs = self._jobs(snapshot.wip)
        self.jobs_by_order = {key: [self.jobs[key, i] for i in range(1, len(order.lines) + 1)]
                              for key, order in self.orders.items()}
        self.inventory = self._inventory(snapshot)

    def fork(self):
        """Copy run-local state while sharing the normalized factory inputs."""
        model = copy(self)
        model.stations = {key: copy(station) for key, station in self.stations.items()}
        model.jobs = {key: copy(job) for key, job in self.jobs.items()}
        model.jobs_by_order = {key: [model.jobs[job.ID_J] for job in jobs] for key, jobs in self.jobs_by_order.items()}
        model.inventory = {key: copy(inv) for key, inv in self.inventory.items()}
        for key, inv in model.inventory.items():
            inv.pending_deliveries = self.inventory[key].pending_deliveries.copy()
        return model

    @staticmethod
    def _table(data, name):
        required = TABLES[name]
        return indexed(getattr(data, name), name, required, required[0])

    def _parts(self, data):
        result = {}
        for identifier, row in self._table(data, "Parts").items():
            routing = sequence(row["routing"], "routing", nonempty=True)
            bom = row["BOM"]
            if not isinstance(bom, Mapping):
                aligned = sequence(bom, "BOM")
                if aligned and len(aligned) != len(routing):
                    raise ValueError(f"BOM for {identifier!r} must align with the routing")
                bom = dict(enumerate(aligned))
            by_step = {}
            for step, amounts in bom.items():
                if not isinstance(step, Integral) or not 0 <= step < len(routing):
                    raise ValueError(f"Invalid BOM routing step {step!r} for {identifier!r}")
                required = quantities(amounts, f"BOM for {identifier!r} at step {step}")
                unknown = required.keys() - self.materials.keys()
                if unknown:
                    raise ValueError(f"Part {identifier!r} references unknown materials {unknown}")
                by_step[step] = required
            result[identifier] = Part(routing, by_step)
        return result

    def _materials(self, data):
        material_rows = self._table(data, "Materials")
        for key, row in material_rows.items():
            suppliers = sequence(row["suppliers"], f"Suppliers for {key!r}")
            if len(set(suppliers)) != len(suppliers) or any(missing(supplier) for supplier in suppliers):
                raise ValueError(f"Supplier identifiers must be distinct and nonempty for {key!r}")
            lead_times = row["DLT"]
            if isinstance(lead_times, Mapping):
                if lead_times.keys() != set(suppliers):
                    raise ValueError(f"Supply DLT for every supplier of material {key!r}")
                lead_times = lead_times.values()
            else:
                lead_times = (lead_times,)
            for value in lead_times:
                number(value.nominal if isinstance(value, Parameter) else value, f"DLT for {key!r}")
            otp = row["OTP"]
            number(otp.nominal if isinstance(otp, Parameter) else otp, f"OTP for {key!r}", positive=True)
            amount = row["q"]
            quantity(amount.nominal if isinstance(amount, Parameter) else amount, f"Purchase quantity for {key!r}", positive=True)
            row["last_order"] = None if missing(row["last_order"]) else timestamp(row["last_order"])
        return material_rows

    def _processes(self, data):
        result = {identifier: Process(row["tp"], row["tsu"], sequence(row["WS_groups"], "WS_groups"))
                  for identifier, row in self._table(data, "Processes").items()}
        for part_id, part in self.parts.items():
            for process_id in part.routing:
                if process_id not in result:
                    raise ValueError(f"Part {part_id!r} references unknown process {process_id!r}")
                result[process_id].unit_time(part_id)
        return result

    def _stations(self):
        result = {}
        for identifier, row in self.station_data.items():
            state = row["availability"]
            if isinstance(state, (bool, np.bool_)):
                state = "available" if state else "unavailable"
            if missing(state) or state not in ("available", "processing", "unavailable"):
                raise ValueError(f"Invalid availability for station {identifier!r}: {state!r}")
            station = Station(identifier, row["WS_group"], state, row.get("previous_part"))
            if state == "unavailable":
                station.t_done = self.start + minutes(row["t_down"], f"t_down for {identifier!r}")
                station.reason = row.get("unavailability_reason")
                if missing(station.reason):
                    station.reason = "INITIAL_DOWNTIME"
            result[identifier] = station
        return result

    def _orders(self, orders, rfq):
        columns = TABLES["Orders"]
        rows = indexed(orders, "Orders", columns, "ID_O")
        if rfq is not None:
            added = indexed(rfq, "RFQ", columns, "ID_O")
            if rows.keys() & added.keys():
                raise ValueError("RFQ IDs must be distinct from existing order IDs")
            if any(row["status"] != "outstanding" for row in added.values()):
                raise ValueError("RFQs must have status 'outstanding'")
            rows.update(added)
        result = {}
        for identifier, row in rows.items():
            if not missing(row["status"]) and row["status"] == "completed":
                continue
            if missing(row["status"]) or row["status"] != "outstanding":
                raise ValueError(f"Invalid order status for {identifier!r}: {row['status']!r}")
            lines = []
            for part_id, amount in sequence(row["lines"], "order lines", nonempty=True):
                if part_id not in self.parts:
                    raise ValueError(f"Unknown part {part_id!r} in order {identifier!r}")
                lines.append((part_id, quantity(amount, f"Quantity in order {identifier!r}", positive=True)))
            result[identifier] = Order(identifier, tuple(lines), timestamp(row["RD"]), timestamp(row["DD"]))
        return result

    def _jobs(self, wip):
        jobs = {}
        for order in self.orders.values():
            for line, (part_id, amount) in enumerate(order.lines, start=1):
                job = Job(order, line, part_id, amount)
                jobs[job.ID_J] = job
        seen = set()
        rows = [] if wip is None else records(wip, "WIP", ("ID_O", "line", "state", "step"))
        for row in rows:
            key = row["ID_O"], row["line"]
            if key in seen or key not in jobs or jobs[key].order.RD > self.start:
                raise ValueError(f"Duplicate, unknown or future job in WIP: {key!r}")
            seen.add(key)
            self._apply_wip(jobs[key], row)
        absent = [key for key, job in jobs.items() if job.order.RD < self.start and key not in seen]
        if absent:
            raise ValueError(f"Missing WIP for orders released before the snapshot: {absent}")
        for station in self.stations.values():
            if station.state == "processing" and station.ID_J is None:
                raise ValueError(f"Missing processing WIP for station {station.ID_WS!r}")
        return jobs

    def _apply_wip(self, job, row):
        part = self.parts[job.ID_pt]
        step = row["step"]
        if not isinstance(step, Integral) or not 0 <= step < len(part.routing):
            raise ValueError(f"Invalid WIP routing step for {job.ID_J!r}: {step!r}")
        job.step, job.state = int(step), row["state"]
        if job.state == "waiting":
            if not missing(row.get("ready_at")):
                job.ready_at = timestamp(row["ready_at"])
                if not job.order.RD <= job.ready_at <= self.start:
                    raise ValueError(f"Invalid WIP ready_at for {job.ID_J!r}")
        elif job.state == "finished":
            job.completed_at = timestamp(row.get("completed_at"))
            if step != len(part.routing) - 1 or not job.order.RD <= job.completed_at <= self.start:
                raise ValueError(f"Invalid WIP completion for {job.ID_J!r}")
        elif job.state == "processing":
            self._apply_processing_wip(job, row, part)
        else:
            raise ValueError(f"Invalid WIP state for {job.ID_J!r}: {job.state!r}")

    def _apply_processing_wip(self, job, row, part):
        station_id = row.get("ID_WS")
        station = self.stations.get(station_id)
        if station is None or station.state == "unavailable" or station.ID_J is not None:
            raise ValueError(f"Invalid WIP workstation for {job.ID_J!r}: {station_id!r}")
        process = self.processes[part.routing[job.step]]
        if station.group not in process.groups:
            raise ValueError(f"WIP job {job.ID_J!r} is assigned to an incompatible station")
        remaining = minutes(row.get("remaining_minutes"), f"WIP remaining time for {job.ID_J!r}")
        completion = self.start + remaining
        shift = self.calendar.next_slot(self.start, remaining)
        prior_boundary = (remaining == pd.Timedelta(0) and any(
            s.t_done == self.start for day in (self.start, self.start - pd.Timedelta(days=1))
            for s in self.calendar.on_date(day.date())
        ))
        if not prior_boundary and (shift is None or shift.t_start > self.start):
            raise ValueError(f"Processing WIP for {job.ID_J!r} cannot finish within the active shift")
        job.ID_WS, job.t_done = station_id, completion
        station.state, station.ID_J, station.t_done = "processing", job.ID_J, completion

    def _inventory(self, snapshot):
        result = {}
        rows = {} if snapshot.inventory is None else indexed(
            snapshot.inventory, "Inventory", ("ID_mat", "cq"), "ID_mat")
        if rows.keys() != self.materials.keys():
            missing_ids = list(self.materials.keys() - rows.keys())
            extra_ids = list(rows.keys() - self.materials.keys())
            raise ValueError(f"Inventory snapshot mismatch: missing {missing_ids}, unknown {extra_ids}")
        for key, row in rows.items():
            next_order = row.get("next_order_time")
            next_order = None if missing(next_order) else timestamp(next_order)
            if next_order is not None and next_order < self.start:
                raise ValueError(f"Past next_order_time for {key!r}; supply the current inventory snapshot")
            result[key] = Inventory(key, quantity(row["cq"], f"Stock for {key!r}"), next_order)
        if snapshot.pending_deliveries is None:
            return result
        pending = indexed(snapshot.pending_deliveries, "PendingDeliveries",
                          ("ID_PO", "ID_mat", "q", "arrival"), "ID_PO")
        for key, row in pending.items():
            material = row["ID_mat"]
            arrival = timestamp(row["arrival"])
            if material not in result or arrival < self.start:
                raise ValueError(f"Unknown material or past arrival for pending delivery {key!r}")
            supplier = None if missing(row.get("supplier")) else row["supplier"]
            result[material].pending_deliveries[key] = Delivery(
                key, quantity(row["q"], f"Quantity for {key!r}", positive=True), arrival, supplier)
        return result


def load_workbook(path, *, start):
    """Load FACTS V0 sheets and optional WIP, Inventory and PendingDeliveries sheets."""
    structured = {
        "FACTS": ("shift_schedule", "shift_exceptions", "priority_rules"),
        "Parts": ("routing", "BOM"),
        "Processes": ("tp", "tsu", "WS_groups"),
        "Orders": ("lines",),
        "Materials": ("suppliers", "DLT"),
    }
    sheets = pd.read_excel(path, sheet_name=None)
    absent = TABLES.keys() - sheets.keys()
    if absent:
        raise ValueError(f"FACTS V0 workbook is missing sheets {sorted(absent)}")
    for sheet, columns in structured.items():
        for column in columns:
            if column in sheets[sheet]:
                sheets[sheet][column] = sheets[sheet][column].map(
                    lambda value: ast.literal_eval(value) if isinstance(value, str) else value)
    return (FactoryData(**{name: sheets[name] for name in TABLES}),
            Snapshot(start, sheets.get("WIP"), sheets.get("Inventory"), sheets.get("PendingDeliveries")))
