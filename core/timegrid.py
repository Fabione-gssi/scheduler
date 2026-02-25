from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

@dataclass(frozen=True)
class TimeGrid:
    start: datetime
    end: datetime
    slot_minutes: int

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("TimeGrid: end must be after start")
        if self.slot_minutes not in (30, 60):
            raise ValueError("TimeGrid: slot_minutes must be 30 or 60")
        # align to slot boundaries for sanity
        if self.start.minute % self.slot_minutes != 0 or self.start.second != 0 or self.start.microsecond != 0:
            raise ValueError("TimeGrid: start must align to slot boundary")
        if self.end.minute % self.slot_minutes != 0 or self.end.second != 0 or self.end.microsecond != 0:
            raise ValueError("TimeGrid: end must align to slot boundary")

    @property
    def slot_delta(self) -> timedelta:
        return timedelta(minutes=self.slot_minutes)

    @property
    def n_slots(self) -> int:
        return int((self.end - self.start) / self.slot_delta)

    def slot_start(self, s: int) -> datetime:
        if not (0 <= s <= self.n_slots):
            raise IndexError("slot index out of range")
        return self.start + s * self.slot_delta

    def window_to_slot_range(self, start: datetime, end: datetime) -> tuple[int, int]:
        """Convert [start, end) into [s0, s1) slot indices. Requires alignment."""
        if start < self.start or end > self.end:
            raise ValueError("Window outside TimeGrid")
        if start.minute % self.slot_minutes != 0 or end.minute % self.slot_minutes != 0:
            raise ValueError("Window not aligned to slot size")
        s0 = int((start - self.start) / self.slot_delta)
        s1 = int((end - self.start) / self.slot_delta)
        return s0, s1
