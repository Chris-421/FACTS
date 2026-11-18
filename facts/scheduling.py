"""Feasible assignments and FIFO/EDD dispatch, independent of the event queue."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import pandas as pd

from .model import minutes, quantities


class ModelingDecisionRequired(ValueError):
    """The simulation reached a choice for which no modeling policy was supplied."""


@dataclass(frozen=True)
class Assignment:
    ID_J: tuple
    ID_WS: object
    ID_proc: object
    duration: pd.Timedelta
    setup_minutes: float
    setup_source: str
    material: Mapping


class Scheduler:
    def __init__(self, rule, priorities, material_at_dispatch: Callable | None):
        if rule not in ("FIFO", "EDD"):
            raise ValueError(f"Unknown scheduling policy: {rule!r}")
        if not isinstance(priorities, Mapping):
            raise ValueError("priority_rules must be a mapping")
        self.rule = rule
        self.priorities = priorities
        self.material_at_dispatch = material_at_dispatch

    def requirements(self, job, part):
        if not part.materials(job.step):
            return {}
        if self.material_at_dispatch is None:
            raise ModelingDecisionRequired(f"Configure material handling for BOM-backed job {job.ID_J!r}")
        return quantities(self.material_at_dispatch(job, part), f"Material for job {job.ID_J!r}")

    @staticmethod
    def operation(job, station, parts, processes):
        process_id = parts[job.ID_pt].routing[job.step]
        process = processes[process_id]
        if station.group not in process.groups:
            return None
        setup, source = process.setup(station.previous_part, job.ID_pt)
        duration = minutes(setup + process.unit_time(job.ID_pt) * float(job.q), "Operation duration")
        return process_id, duration, setup, source

    def feasible(self, jobs, stations, inventories, parts, processes, remaining, *, shortage=None):
        options = {}
        too_long = {}
        for job in jobs.values():
            if job.state != "waiting":
                continue
            compatible = []
            for station in stations.values():
                if station.state != "available":
                    continue
                operation = self.operation(job, station, parts, processes)
                if operation is not None:
                    compatible.append((station.ID_WS, operation))
            if compatible:
                material = self.requirements(job, parts[job.ID_pt])
                if self.has_stock(material, inventories):
                    for station_id, operation in compatible:
                        if operation[1] <= remaining:
                            assignment = Assignment(job.ID_J, station_id, *operation, material)
                            options.setdefault(job.ID_J, []).append(assignment)
                        else:
                            too_long.setdefault(station_id, []).append(job.ID_J)
                elif shortage is not None:
                    shortage(job, material)
        for key, assignments in options.items():
            options[key] = [a for a in assignments if self._may_backfill(key, too_long.get(a.ID_WS, ()), jobs)]
        return {key: assignments for key, assignments in options.items() if assignments}

    def _may_backfill(self, key, skipped_jobs, jobs):
        contenders = [other for other in skipped_jobs if other != key]
        if not contenders or self.priorities.get("backfill", True):
            return True
        return all(self._primary(jobs[other]) >= self._primary(jobs[key]) for other in contenders)

    @staticmethod
    def has_stock(material, inventories):
        for key, amount in material.items():
            if key not in inventories:
                raise ValueError(f"Material policy references unknown material {key!r}")
            if inventories[key].cq < amount:
                return False
        return True

    def schedule(self, options, jobs, stations, inventories):
        options = dict(options)
        stock = {key: inv.cq for key, inv in inventories.items()}
        assignments = []
        while options:
            candidates = self._top_jobs(options, jobs, stock)
            if len(candidates) > 1 and self._compete(candidates, options, stock):
                rank = self.priorities.get("job_tiebreak")
                if rank is not None:
                    job_id = self._choose(candidates, rank, jobs, "job_tiebreak")
                else:
                    best = min(min(a.duration for a in options[key]) for key in candidates)
                    fastest = [key for key in candidates if any(a.duration == best for a in options[key])]
                    job_id = min(fastest, key=lambda key: self._queue_entry(jobs[key])) if len(fastest) > 1 else fastest[0]
            else:
                job_id = candidates[0]
            choices = {choice.ID_WS: choice for choice in options.pop(job_id)}
            station_rank = self.priorities.get("station_selection")
            if station_rank is None:
                station_id = min(choices, key=lambda key: choices[key].duration)
            else:
                station_id = self._choose(list(choices), station_rank, stations, "station_selection")
            selection = choices[station_id]
            assignments.append(selection)
            for key, amount in selection.material.items():
                stock[key] -= amount
            options = {key: [a for a in choices if a.ID_WS != station_id]
                       for key, choices in options.items()
                       if all(stock[m] >= q for m, q in choices[0].material.items())}
            options = {key: choices for key, choices in options.items() if choices}
        return assignments

    def _top_jobs(self, options, jobs, stock):
        if len(options) == 1 or not self._compete(options, options, stock):
            return list(options)
        rank = {key: self._primary(jobs[key]) for key in options}
        best = min(rank.values())
        return [key for key in rank if rank[key] == best]

    def _primary(self, job):
        if self.rule == "EDD":
            return job.order.DD
        basis = self.priorities.get("fifo_basis", "queue_entry")
        if basis == "release":
            return job.order.RD
        if basis == "queue_entry":
            return self._queue_entry(job)
        raise ModelingDecisionRequired("Set priority_rules['fifo_basis'] for competing FIFO jobs")

    @staticmethod
    def _queue_entry(job):
        if job.ready_at is None:
            raise ValueError(f"Dispatch priority requires ready_at for competing waiting WIP {job.ID_J!r}")
        return job.ready_at

    @staticmethod
    def _compete(candidates, options, stock):
        occupied = set()
        total = {}
        for key in candidates:
            stations = {candidate.ID_WS for candidate in options[key]}
            if occupied & stations:
                return True
            occupied.update(stations)
            for material, amount in options[key][0].material.items():
                total[material] = total.get(material, 0) + amount
                if total[material] > stock[material]:
                    return True
        return False

    @staticmethod
    def _choose(candidates, rank, entities, name):
        if len(candidates) == 1:
            return candidates[0]
        if rank == "id":
            return min(candidates)
        if callable(rank):
            ranks = {key: rank(entities[key]) for key in candidates}
            best = min(ranks.values())
            winners = [key for key, value in ranks.items() if value == best]
            if len(winners) == 1:
                return winners[0]
        raise ModelingDecisionRequired(f"Set an unambiguous priority_rules[{name!r}] for {candidates!r}")
