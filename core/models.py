from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional, Sequence

# -------------------------
# Domain primitives
# -------------------------

@dataclass(frozen=True)
class Window:
    """A half-open time window [start, end)."""
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"Invalid Window: end <= start ({self.start} -> {self.end})")


@dataclass(frozen=True)
class Segment:
    """A scheduled segment for a task on one or more resources."""
    task_id: str
    resource_ids: tuple[str, ...]
    start: datetime
    end: datetime
    note: str = ""

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"Invalid Segment: end <= start ({self.start} -> {self.end})")


# -------------------------
# Resources & requirements
# -------------------------

@dataclass
class Resource:
    id: str
    name: str
    skills: set[str] = field(default_factory=set)
    unavailability: list[Window] = field(default_factory=list)


@dataclass(frozen=True)
class SkillRequirement:
    skill: str
    count: int

    def __post_init__(self) -> None:
        if self.count <= 0:
            raise ValueError("SkillRequirement.count must be > 0")


@dataclass(frozen=True)
class TaskRequirement:
    """Multi-resource requirement for a task.

    - fixed_resources: resources that MUST participate whenever the task is active
    - skill_requirements: (skill, count) bundles; each 'count' expands to that many role-units
      whose participants are chosen from the pool of resources having that skill.
    """
    fixed_resources: tuple[str, ...] = ()
    skill_requirements: tuple[SkillRequirement, ...] = ()


# -------------------------
# Tasks & dependencies
# -------------------------

@dataclass(frozen=True)
class Dependency:
    """A precedence relation with lag in slots.

    Semantics (finish-to-start with lag):
      start(successor) >= end(predecessor) + lag_slots

    If lag_slots is negative, successor may overlap the predecessor.
    """
    predecessor_id: str
    lag_slots: int = 0


@dataclass
class Task:
    id: str
    name: str
    duration_slots: int
    priority: int = 3  # 1..5
    due_slot: Optional[int] = None
    earliest_slot: Optional[int] = None
    splittable: bool = True
    max_splits: int = 4  # number of splits (segments = max_splits+1)
    requirement: TaskRequirement = field(default_factory=TaskRequirement)
    dependencies: list[Dependency] = field(default_factory=list)

    # Preassigned segments can be HARD (locked) or SOFT (preferred)
    preassigned_hard: list[Segment] = field(default_factory=list)
    preassigned_soft: list[Segment] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.duration_slots <= 0:
            raise ValueError(f"Task.duration_slots must be > 0 (task {self.id})")
        if not (1 <= self.priority <= 5):
            raise ValueError(f"Task.priority must be 1..5 (task {self.id})")
        if self.max_splits < 0:
            raise ValueError(f"Task.max_splits must be >= 0 (task {self.id})")


# -------------------------
# Overrides / weights
# -------------------------

@dataclass(frozen=True)
class LockOverride:
    """Hard lock: enforce that the given task uses exactly these resources in this window."""
    task_id: str
    resource_ids: tuple[str, ...]
    window: Window


@dataclass(frozen=True)
class BanOverride:
    """Hard ban: forbid task assignment on (resource, window)."""
    task_id: str
    resource_id: str
    window: Window


@dataclass
class Overrides:
    locks: list[LockOverride] = field(default_factory=list)
    bans: list[BanOverride] = field(default_factory=list)


@dataclass(frozen=True)
class Weights:
    w_deadline: int = 50
    w_fragmentation: int = 30
    w_nice: int = 20  # NEW

    def __post_init__(self) -> None:
        for name, v in (
            ("w_deadline", self.w_deadline),
            ("w_fragmentation", self.w_fragmentation),
            ("w_nice", self.w_nice),
        ):
            if not (0 <= v <= 100):
                raise ValueError(f"{name} must be in 0..100")

@dataclass(frozen=True)
class SolveLimits:
    max_time_seconds: int = 20
    num_search_workers: int = 8

    def __post_init__(self) -> None:
        if self.max_time_seconds <= 0:
            raise ValueError("max_time_seconds must be > 0")
        if self.num_search_workers <= 0:
            raise ValueError("num_search_workers must be > 0")


# -------------------------
# Problem & solution
# -------------------------

@dataclass
class Problem:
    start: datetime
    end: datetime
    slot_minutes: int
    resources: dict[str, Resource]
    tasks: dict[str, Task]
    # Optional: common work calendar (Mon-Fri 9-18) is assumed by builder; solver sees availability mask.
    availability_mask: dict[str, list[bool]]  # resource_id -> length S list
    task_allowed_mask: dict[str, list[bool]]  # task_id -> length S list
    task_allowed_mask: dict[str, list[bool]]  # task_id -> length S list (hard)
    task_nice_mask: dict[str, list[bool]]     # task_id -> length S list (soft preference)

    def num_slots(self) -> int:
        return len(next(iter(self.availability_mask.values()))) if self.availability_mask else 0


@dataclass
class Solution:
    segments: list[Segment]
    metrics: dict[str, float] = field(default_factory=dict)
    status: str = "UNKNOWN"
    objective_value: Optional[float] = None
    infeasible_reason: str = ""

    def segments_for_resource(self, resource_id: str) -> list[Segment]:
        return [s for s in self.segments if resource_id in s.resource_ids]
