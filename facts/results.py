"""State snapshots, event logs, and per-run result tables."""

from dataclasses import asdict, dataclass

import pandas as pd


@dataclass
class RunResult:
    initial: dict[str, pd.DataFrame]
    final: dict[str, pd.DataFrame]
    event_log: pd.DataFrame
    sampled_inputs: pd.DataFrame
    jobs: pd.DataFrame
    orders: pd.DataFrame
    station_shifts: pd.DataFrame
    stations: pd.DataFrame
    experiment: pd.DataFrame
    diagnostics: pd.DataFrame
    configuration: dict


class Journal:
    event_columns = ("scenario_id", "run_id", "event_id", "timestamp", "entity_type", "entity_id",
                     "event_type", "state_before", "state_after", "related_entity_id", "details")
    draw_columns = ("scenario_id", "run_id", "draw_id", "entity_type", "entity_id", "parameter",
                    "sampled_value", "timestamp", "details")

    def __init__(self, scenario, run_id):
        self.keys = (scenario, run_id)
        self.events = []
        self.draws = []

    def event(self, now, entity, key, kind, before=None, after=None, related=None, **details):
        self.events.append((*self.keys, len(self.events) + 1, now, entity, key, kind,
                            before, after, related, details))

    def draw(self, now, entity, key, parameter, value, **details):
        self.draws.append((*self.keys, len(self.draws) + 1, entity, key, parameter, value, now, details))

    def table(self, rows, columns):
        columns = ["scenario_id", "run_id", *columns]
        return pd.DataFrame([(*self.keys, *row) for row in rows], columns=columns)


def snapshot(engine):
    factory = engine.factory
    factory_frame = pd.DataFrame([{"Shift_ID": factory.Shift_ID, "shift_number": factory.shift_number,
                                   "t_start": factory.t_start, "t_done": factory.t_done, "active": factory.active}])
    factory_frame.attrs["snapshot_time"] = engine.now
    model = engine.model
    uncertainty = engine.uncertainty
    stations = []
    for station in model.stations.values():
        clock = uncertainty.clocks.get(station.ID_WS)
        stations.append({**asdict(station), "time_to_failure_operating": clock.remaining if clock else None,
                         "generic_loss_budget_remaining": uncertainty.budget.get(station.ID_WS) if factory.active else None})
    return {
        "F": factory_frame,
        "J": pd.DataFrame([(j.ID_J, j.ID_O, j.ID_pt, j.q, j.state, j.step, j.t_done, j.ID_WS,
                            j.ready_at, j.completed_at) for j in model.jobs.values()],
                          columns=["ID_J", "ID_O", "ID_pt", "q", "state", "step", "t_done", "ID_WS",
                                   "ready_at", "completed_at"]),
        "S": pd.DataFrame(stations, columns=["ID_WS", "group", "state", "previous_part", "t_done", "ID_J", "reason",
                                             "time_to_failure_operating", "generic_loss_budget_remaining"]),
        "Inv": pd.DataFrame([(i.ID_mat, i.cq, i.next_order_time,
                              tuple(asdict(d) for d in i.pending_deliveries.values())) for i in model.inventory.values()],
                            columns=["ID_mat", "cq", "next_order_time", "pending_deliveries"]),
    }
