"""Read-only queue and material observations at stable DES clock intervals."""

from collections import Counter, defaultdict
from itertools import chain

import pandas as pd

from .model import minute_count


class FlowObserver:
    def __init__(self, rfq_id, *, trace_requirements=False, until=None):
        self.rfq_id = rfq_id
        self.trace_requirements = trace_requirements
        self.until = until
        self.stations = defaultdict(Counter)
        self.groups = defaultdict(Counter)
        self.requirements = []
        self.scheduled = 0.

    def observe(self, engine, end):
        now = engine.now
        if self.until is not None:
            end = min(end, self.until)
            if end <= now:
                return
        model = engine.model
        elapsed = minute_count(end - now)
        self.scheduled += elapsed
        queues = Counter()
        wip = Counter()
        by_group = defaultdict(list)
        for station in model.stations.values():
            by_group[station.group].append(station)
            if station.state == "processing":
                wip[("station", station.ID_WS)] += 1
                wip[("group", station.group)] += 1
        for job in model.jobs.values():
            if job.state != "waiting":
                continue
            operations = {}
            process = model.processes[model.parts[job.ID_pt].routing[job.step]]
            for group in process.groups:
                for station in by_group[group]:
                    _, duration, _, _ = engine.scheduler.operation(job, station, model.parts, model.processes)
                    possible = max(0., minute_count(min(end, engine.factory.t_done - duration) - now))
                    operations[station.ID_WS] = possible
            if not operations:
                continue
            bundle = engine.scheduler.requirements(job, model.parts[job.ID_pt])
            material_ready = engine.scheduler.has_stock(bundle, model.inventory)
            is_rfq = job.ID_O == self.rfq_id
            if self.trace_requirements and is_rfq:
                for material, quantity in bundle.items():
                    available = model.inventory[material].cq
                    self.requirements.append({"job": job.ID_J, "step": job.step, "material": material,
                                              "required_at": job.ready_at, "from": now, "until": end,
                                              "quantity_required": quantity, "stock": available,
                                              "short_wait_working_minutes": elapsed if available < quantity else 0.})
            available_fit = max((duration for key, duration in operations.items()
                                 if model.stations[key].state == "available"), default=0.)
            best_by_group = defaultdict(float)
            for key, fit in operations.items():
                station = model.stations[key]
                best_by_group[station.group] = max(best_by_group[station.group], fit)
                row = self.stations[key]
                wait = self._add(row, elapsed, fit, available_fit, material_ready, is_rfq)
                if is_rfq and station.reason == "MACHINE_FAILURE":
                    row["rfq_failure_overlap_minutes"] += wait
                queues[("station", key)] += 1
            for key, fit in best_by_group.items():
                self._add(self.groups[key], elapsed, fit, available_fit, material_ready, is_rfq)
                queues[("group", key)] += 1
        for (kind, key), count in (queues + wip).items():
            row = (self.stations if kind == "station" else self.groups)[key]
            row["peak_wip"] = max(row["peak_wip"], count)
            row["wip_job_minutes"] += count * elapsed
        for (kind, key), count in queues.items():
            row = (self.stations if kind == "station" else self.groups)[key]
            row["peak_queue"] = max(row["peak_queue"], count)

    @staticmethod
    def _add(row, elapsed, fit, available, stocked, is_rfq):
        capacity_wait = max(0., fit - available) if stocked else 0.
        row["queue_job_minutes"] += elapsed
        row["capacity_wait_job_minutes"] += capacity_wait
        if is_rfq:
            row["rfq_queue_minutes"] += elapsed
            row["rfq_capacity_wait_minutes"] += capacity_wait
            row["rfq_material_wait_minutes"] += elapsed if not stocked else 0.
            row["rfq_shift_fit_wait_minutes"] += elapsed - fit if stocked else 0.
            row["rfq_dispatch_wait_minutes"] += min(fit, available) if stocked else 0.
        return capacity_wait

    def station_and_group_rows(self, run, stations):
        groups = defaultdict(Counter)
        station_metrics = ("observed_shift_time", "scheduled_shift_time", "processing_time", "generic_unavailability_time",
                           "machine_downtime", "initial_downtime", "idle_no_work", "idle_shift_constraint",
                           "idle_material", "idle_dispatch", "completed_operations")
        for station in run.stations.itertuples(index=False):
            key = station.ID_WS
            group = stations[key].group
            operational = {name: getattr(station, name) for name in station_metrics}
            groups[group].update(operational)
            yield {"resource_type": "station", "resource": key, "group": group, "observed_minutes": self.scheduled,
                   **operational, **self.stations[key]}
        for key, operational in groups.items():
            yield {"resource_type": "group", "resource": key, "group": key, "observed_minutes": self.scheduled,
                   **operational, **self.groups[key]}


def summarize_capacity(paths):
    """Pool scheduled time and queue areas, with mean waiting per trajectory."""
    rows = []
    for (kind, key), group in paths.groupby(["resource_type", "resource"], sort=False):
        stat = {"resource_type": kind, "resource": key, "group": group["group"].iloc[0]}
        station_seen = group.observed_shift_time.sum()
        observed = group.observed_minutes.sum()
        queue_fields = ("queue_job_minutes", "wip_job_minutes", "peak_wip", "peak_queue", "capacity_wait_job_minutes",
                        "rfq_queue_minutes", "rfq_capacity_wait_minutes", "rfq_material_wait_minutes",
                        "rfq_shift_fit_wait_minutes", "rfq_dispatch_wait_minutes")
        for name in chain(queue_fields, ("scheduled_shift_time", "observed_shift_time", "processing_time",
                                        "completed_operations", "generic_unavailability_time", "machine_downtime",
                                        "initial_downtime", "idle_no_work", "idle_shift_constraint", "idle_material", "idle_dispatch")):
            stat[f"mean_{name}"] = float(group[name].fillna(0).mean()) if name in group else 0.
        stat.update({"processing_utilization": float(group.processing_time.sum() / station_seen) if station_seen else None,
                     "mean_queue_length": float(group.get("queue_job_minutes", pd.Series(dtype=float)).sum() / observed) if observed else None,
                     "mean_wip": float(group.get("wip_job_minutes", pd.Series(dtype=float)).sum() / observed) if observed else None,
                     "max_sampled_queue": int(group["peak_queue"].fillna(0).max()) if "peak_queue" in group else 0,
                     "max_sampled_wip": int(group["peak_wip"].fillna(0).max()) if "peak_wip" in group else 0})
        stat["mean_unavailable_minutes"] = sum(stat[f"mean_{name}"] for name in (
            "generic_unavailability_time", "machine_downtime", "initial_downtime"))
        stat["mean_idle_minutes"] = sum(stat[f"mean_{name}"] for name in (
            "idle_no_work", "idle_shift_constraint", "idle_material", "idle_dispatch"))
        rows.append(stat)
    return rows
