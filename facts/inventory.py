"""Purchase orders and explicitly configured consumption/replenishment rules."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import count

from .calendar import timestamp
from .events import Event
from .model import Delivery, Parameter, minutes, number, quantity, sequence
from .scheduling import ModelingDecisionRequired


def bundle_at_start(job, part):
    return {material: per_unit * job.q for material, per_unit in part.materials(job.step).items()}


@dataclass(frozen=True)
class InventoryRules:
    material_at_dispatch: Callable = bundle_at_start
    shortage: str = "wait"
    initialize_order: Callable | None = None
    select_supplier: Callable | None = None
    sample_otp: bool = False


class StockShortage(Exception):
    def __init__(self, job_id, materials):
        self.job_id = job_id
        self.materials = materials
        super().__init__(f"Job {job_id!r} is short of materials {materials!r}")


class Purchasing:
    def __init__(self, rules):
        self.rules = rules
        if rules.shortage not in (None, "wait", "stop"):
            raise ValueError("Inventory shortage policy must be 'wait' or 'stop'")
        self.sequence = count(1)

    def shortage(self, engine, job, requirements):
        if self.rules.shortage is None:
            raise ModelingDecisionRequired(f"Configure a stock-shortage policy for {job.ID_J!r}")
        if self.rules.shortage == "stop":
            missing = [key for key, amount in requirements.items() if engine.model.inventory[key].cq < amount]
            raise StockShortage(job.ID_J, missing)

    def initialize(self, engine):
        self.purchase_ids = {purchase_id for inv in engine.model.inventory.values()
                             for purchase_id in inv.pending_deliveries}
        for key, inv in engine.model.inventory.items():
            for delivery in inv.pending_deliveries.values():
                engine.queue.add(delivery.arrival, Event.MATERIAL_DELIVERY, (key, delivery.ID_PO))
            when = inv.next_order_time
            if when is None and self.rules.initialize_order is not None:
                when = self.rules.initialize_order(engine.model.materials[key], engine.now)
            self._schedule_order(engine, inv, when, allow_now=True)

    @staticmethod
    def _schedule_order(engine, inv, when, *, allow_now=False):
        if when is None:
            return
        when = timestamp(when)
        if when < engine.now or (when == engine.now and not allow_now):
            raise ValueError("The next replenishment order must advance the clock")
        if inv.next_order_time is not None and inv.next_order_time != when:
            raise ValueError(f"Multiple next order times for {inv.ID_mat!r}")
        inv.next_order_time = when
        engine.queue.add(when, Event.MATERIAL_ORDER, inv.ID_mat)

    @staticmethod
    def nominal(value):
        return value.nominal if isinstance(value, Parameter) else value

    def place(self, engine, key):
        inv = engine.model.inventory[key]
        terms = engine.model.materials[key]
        suppliers = sequence(terms["suppliers"], f"Suppliers for {key!r}")
        if self.rules.select_supplier is not None:
            supplier = self.rules.select_supplier(key, suppliers, engine.now)
            if supplier not in suppliers:
                raise ValueError(f"Unknown selected supplier for {key!r}: {supplier!r}")
        elif len(suppliers) == 1:
            supplier = suppliers[0]
        else:
            raise ModelingDecisionRequired(f"Configure supplier selection for material {key!r}")
        amount = quantity(self.nominal(terms["q"]), f"Purchase quantity for {key!r}", positive=True)
        lead_time_terms = terms["DLT"][supplier] if isinstance(terms["DLT"], Mapping) else terms["DLT"]
        nominal = number(self.nominal(lead_time_terms), f"Nominal DLT for {key!r}")
        purchase_id = ("simulated", next(self.sequence))
        while purchase_id in self.purchase_ids:
            purchase_id = ("simulated", next(self.sequence))
        self.purchase_ids.add(purchase_id)
        lead_time = engine.uncertainty.lead_time(engine, key, supplier, purchase_id, lead_time_terms, nominal)
        arrival = engine.now + minutes(lead_time, f"Delivery lead time for {key!r}")
        interval = engine.uncertainty.order_interval(engine, key, terms["OTP"], self.rules.sample_otp)
        delivery = Delivery(purchase_id, amount, arrival, supplier, engine.now, nominal)
        inv.next_order_time = None
        inv.pending_deliveries[purchase_id] = delivery
        engine.queue.add(arrival, Event.MATERIAL_DELIVERY, (key, purchase_id))
        engine.journal.event(engine.now, "inventory", key, "material_order", related=purchase_id,
                             q=amount, arrival=arrival, supplier=supplier, lead_time_minutes=lead_time,
                             order_interval_minutes=interval)
        self._schedule_order(engine, inv, engine.now + minutes(interval, f"Ordering interval for {key!r}"))

    def deliver(self, engine, key, purchase_id):
        inv = engine.model.inventory[key]
        previous = inv.cq
        delivery = inv.receive(purchase_id)
        engine.journal.event(engine.now, "inventory", key, "material_delivery", previous, inv.cq,
                             purchase_id, q=delivery.q, supplier=delivery.supplier)
