"""Stable event ordering for the factory clock."""

from enum import IntEnum
from heapq import heappop, heappush
from itertools import count


class Event(IntEnum):
    OPERATION_DONE = 0
    STATION_RECOVERY = 1
    SHIFT_END = 2
    SHIFT_START = 3
    ORDER_RELEASE = 4
    MATERIAL_DELIVERY = 5
    MATERIAL_ORDER = 6
    MACHINE_FAILURE = 7


class EventQueue:
    def __init__(self):
        self._heap = []
        self._sequence = count()

    def add(self, when, event, payload=None):
        heappush(self._heap, (when, event, next(self._sequence), payload))

    @property
    def next_time(self):
        return self._heap[0][0] if self._heap else None

    def pop(self):
        when, event, _, payload = heappop(self._heap)
        return when, event, payload
