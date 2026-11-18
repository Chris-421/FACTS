"""Resolve weekly shifts and date overrides to concrete calendar occurrences."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from numbers import Integral

import pandas as pd


def timestamp(value):
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is not None:
        raise ValueError(f"Expected a local, timezone-naive timestamp, got {value!r}")
    return result.as_unit("ns")


@dataclass(frozen=True)
class Shift:
    shift_number: int
    Shift_ID: str
    t_start: pd.Timestamp
    t_done: pd.Timestamp


@dataclass
class FactoryState:
    shift: Shift | None
    active: bool = False

    @property
    def Shift_ID(self):
        return self.shift.Shift_ID if self.shift else None

    @property
    def shift_number(self):
        return self.shift.shift_number if self.shift else None

    @property
    def t_start(self):
        return self.shift.t_start if self.shift else None

    @property
    def t_done(self):
        return self.shift.t_done if self.shift else None


class ShiftCalendar:
    def __init__(self, weekly: Sequence, exceptions: Mapping):
        self.weekly = {day: [] for day in range(7)}
        self.exceptions = {}
        for entry in weekly:
            day = entry["weekday"]
            if not isinstance(day, Integral) or day not in self.weekly:
                raise ValueError("Shift weekday must be an integer from 0 (Monday) to 6")
            self.weekly[day].append(self._definition(entry))
        sample_monday = date(2026, 1, 5)
        ordered = [shift for offset in range(7)
                   for shift in self.on_date(sample_monday + timedelta(days=offset))]
        if [shift.shift_number for shift in ordered] != list(range(len(ordered))):
            raise ValueError("Weekly shift numbers must start at zero and follow calendar order")
        self.recurs = bool(ordered)
        for day, entries in exceptions.items():
            day = timestamp(day).date()
            if day in self.exceptions:
                raise ValueError(f"Duplicate shift exception for {day}")
            self.exceptions[day] = [self._definition(entry) for entry in entries]
        dates_to_check = {sample_monday + timedelta(days=i) for i in range(8)}
        for day in self.exceptions:
            dates_to_check.update(day + timedelta(days=i) for i in (-1, 0, 1))
        for day in dates_to_check:
            shifts = self.on_date(day) + self.on_date(day + timedelta(days=1))
            for previous, current in zip(shifts, shifts[1:]):
                if previous.t_done > current.t_start:
                    raise ValueError(f"Overlapping factory shifts at {current.t_start}")

    @staticmethod
    def _definition(entry):
        def clock(value):
            if isinstance(value, str):
                value = time.fromisoformat(value)
            if not isinstance(value, time) or value.tzinfo is not None:
                raise ValueError(f"Expected a local shift time, got {value!r}")
            return value

        number = entry["shift_number"]
        if not isinstance(number, Integral) or number < 0:
            raise ValueError("shift_number must be a nonnegative integer")
        start, end = clock(entry["start"]), clock(entry["end"])
        if start == end:
            raise ValueError("A shift must have different start and end times")
        return number, entry["Shift_ID"], start, end

    def on_date(self, day):
        result = []
        for number, shift_id, start, end in self.exceptions.get(day, self.weekly[day.weekday()]):
            end_day = day + timedelta(days=int(end < start))
            result.append(Shift(number, shift_id, timestamp(datetime.combine(day, start)),
                                timestamp(datetime.combine(end_day, end))))
        return sorted(result, key=lambda shift: shift.t_start)

    def next_slot(self, when, duration=pd.Timedelta(0)):
        """Return the first occurrence with space for duration at or after when."""
        day = when.date() - timedelta(days=1)  # an overnight shift may already be active
        if self.recurs:
            last_override = max((d for d in self.exceptions if d >= day), default=day)
            final_day = max(day, last_override) + timedelta(days=8)
            days = (day + timedelta(days=i) for i in range((final_day - day).days + 1))
        else:
            days = (d for d in sorted(self.exceptions) if d >= day)
        for current_day in days:
            for shift in self.on_date(current_day):
                if when < shift.t_done and max(when, shift.t_start) + duration <= shift.t_done:
                    return shift
        return None

    def shift_time_between(self, start, end):
        day = start.date() - timedelta(days=1)
        total = pd.Timedelta(0)
        while day <= end.date():
            for shift in self.on_date(day):
                total += max(pd.Timedelta(0), min(shift.t_done, end) - max(shift.t_start, start))
            day += timedelta(days=1)
        return total
