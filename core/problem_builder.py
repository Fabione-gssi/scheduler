from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterable

from .excel_io import ParsedInput, parse_dependencies_cell, parse_list_cell, parse_skillreq_cell, segment_from_row
from .models import Problem, Resource, Task, TaskRequirement, Window
from .timegrid import TimeGrid


@dataclass(frozen=True)
class BuildConfig:
    slot_minutes: int
    horizon_start: datetime
    horizon_end: datetime

    work_start: time = time(9, 0)
    work_end: time = time(18, 0)
    work_days: set[int] = frozenset({0, 1, 2, 3, 4})  # Mon..Fri


def _align_dt(dt_in: datetime, slot_minutes: int) -> datetime:
    # Force alignment by rounding DOWN to slot boundary (builder is strict but helpful)
    minute = (dt_in.minute // slot_minutes) * slot_minutes
    return dt_in.replace(minute=minute, second=0, microsecond=0)


def _common_work_slots(grid: TimeGrid, cfg: BuildConfig) -> list[bool]:
    allowed = [False] * grid.n_slots
    for s in range(grid.n_slots):
        st = grid.slot_start(s)
        d = st.date()
        if st.weekday() not in cfg.work_days:
            continue
        if not (cfg.work_start <= st.time() < cfg.work_end):
            continue
        # ensure slot fully fits into work_end
        en = st + grid.slot_delta
        if en.time() > cfg.work_end and en.date() == d:
            continue
        allowed[s] = True
    return allowed


def _apply_unavailability(mask: list[bool], grid: TimeGrid, windows: Iterable[Window]) -> list[bool]:
    out = mask[:]
    for w in windows:
        s0, s1 = grid.window_to_slot_range(w.start, w.end)
        for s in range(s0, s1):
            out[s] = False
    return out


def build_problem(parsed: ParsedInput, cfg: BuildConfig) -> Problem:
    # Align horizon
    start = _align_dt(cfg.horizon_start, cfg.slot_minutes)
    end = _align_dt(cfg.horizon_end, cfg.slot_minutes)
    if end <= start:
        raise ValueError("Horizon end must be after start")

    grid = TimeGrid(start=start, end=end, slot_minutes=cfg.slot_minutes)

    # Resources
    resources: dict[str, Resource] = {}
    for _, row in parsed.resources_df.iterrows():
        rid = str(row["ResourceID"]).strip()
        if not rid:
            continue
        if rid in resources:
            raise ValueError(f"Duplicate ResourceID: {rid}")
        name = str(row.get("Name", rid)).strip() or rid
        skills = set(parse_list_cell(row.get("Skills")))
        resources[rid] = Resource(id=rid, name=name, skills=skills)

    if not resources:
        raise ValueError("No resources provided.")

    # Unavailability
    if not parsed.unavailability_df.empty:
        for _, row in parsed.unavailability_df.iterrows():
            rid = str(row.get("ResourceID", "")).strip()
            if not rid:
                continue
            if rid not in resources:
                raise ValueError(f"Unavailability references unknown ResourceID '{rid}'")
            st = row.get("StartDateTime")
            en = row.get("EndDateTime")
            if st is None or en is None or str(st) == "NaT" or str(en) == "NaT":
                raise ValueError(f"Unavailability row has invalid datetimes for ResourceID '{rid}'")
            st = st.to_pydatetime() if hasattr(st, "to_pydatetime") else st
            en = en.to_pydatetime() if hasattr(en, "to_pydatetime") else en
            # strict: must be aligned and inside horizon
            st = _align_dt(st, cfg.slot_minutes)
            en = _align_dt(en, cfg.slot_minutes)
            if st < start or en > end:
                raise ValueError(f"Unavailability window outside horizon for ResourceID '{rid}': {st} -> {en}")
            resources[rid].unavailability.append(Window(start=st, end=en))

    # Build availability mask per resource: common work calendar minus unavailability
    common_mask = _common_work_slots(grid, cfg)
    availability_mask: dict[str, list[bool]] = {}
    for rid, res in resources.items():
        availability_mask[rid] = _apply_unavailability(common_mask, grid, res.unavailability)

    # Tasks
    tasks: dict[str, Task] = {}
    for _, row in parsed.tasks_df.iterrows():
        tid = str(row["TaskID"]).strip()
        if not tid:
            continue
        if tid in tasks:
            raise ValueError(f"Duplicate TaskID: {tid}")
        name = str(row.get("Name", tid)).strip() or tid

        dur_h = row.get("DurationHours")
        if dur_h is None or (isinstance(dur_h, float) and math.isnan(dur_h)):
            raise ValueError(f"Task '{tid}' has invalid DurationHours")
        dur_slots = int(math.ceil((float(dur_h) * 60) / cfg.slot_minutes))
        if dur_slots <= 0:
            raise ValueError(f"Task '{tid}' has non-positive duration")

        priority = int(row.get("Priority", 3))
        splittable_val = str(row.get("Splittable", "Y")).strip().upper()
        splittable = splittable_val not in ("N", "NO", "FALSE", "0")

        max_splits_raw = row.get("MaxSplits")
        if max_splits_raw is None or (isinstance(max_splits_raw, float) and math.isnan(max_splits_raw)):
            max_splits = 4 if splittable else 0
        else:
            max_splits = int(max_splits_raw)
            if not splittable:
                max_splits = 0

        # Requirements
        fixed_resources = tuple(parse_list_cell(row.get("FixedResources")))
        for rid in fixed_resources:
            if rid not in resources:
                raise ValueError(f"Task '{tid}' references unknown fixed resource '{rid}'")

        skill_reqs = tuple(parse_skillreq_cell(row.get("SkillReq")))
        # Validate skill pools are non-empty
        for sr in skill_reqs:
            pool = [r for r in resources.values() if sr.skill in r.skills]
            if len(pool) < sr.count:
                raise ValueError(
                    f"Task '{tid}' requires {sr.count}x skill '{sr.skill}', but only {len(pool)} resources have it."
                )

        requirement = TaskRequirement(fixed_resources=fixed_resources, skill_requirements=skill_reqs)

        # Due / earliest
        due_dt = row.get("DueDateTime")
        if due_dt is not None and str(due_dt) != "NaT":
            due_dt = due_dt.to_pydatetime() if hasattr(due_dt, "to_pydatetime") else due_dt
            due_dt = _align_dt(due_dt, cfg.slot_minutes)
            if not (start <= due_dt <= end):
                raise ValueError(f"Task '{tid}' DueDateTime is outside horizon: {due_dt}")
            due_slot = grid.window_to_slot_range(due_dt, due_dt)[0]  # start==end -> 0-width, use mapping trick
        else:
            due_slot = None

        earliest_dt = row.get("EarliestStart")
        if earliest_dt is not None and str(earliest_dt) != "NaT":
            earliest_dt = earliest_dt.to_pydatetime() if hasattr(earliest_dt, "to_pydatetime") else earliest_dt
            earliest_dt = _align_dt(earliest_dt, cfg.slot_minutes)
            if earliest_dt < start or earliest_dt > end:
                raise ValueError(f"Task '{tid}' EarliestStart is outside horizon: {earliest_dt}")
            earliest_slot = grid.window_to_slot_range(earliest_dt, earliest_dt)[0]
        else:
            earliest_slot = None

        # Dependencies (lag in hours can be negative)
        deps = parse_dependencies_cell(row.get("Dependencies"), cfg.slot_minutes)

        t = Task(
            id=tid,
            name=name,
            duration_slots=dur_slots,
            priority=priority,
            due_slot=due_slot,
            earliest_slot=earliest_slot,
            splittable=splittable,
            max_splits=max_splits,
            requirement=requirement,
            dependencies=deps,
        )
        tasks[tid] = t

    if not tasks:
        raise ValueError("No tasks provided.")

    # Semantic validation: dependencies reference existing tasks
    for t in tasks.values():
        for d in t.dependencies:
            if d.predecessor_id not in tasks:
                raise ValueError(f"Task '{t.id}' depends on unknown predecessor '{d.predecessor_id}'")

    # Preassigned (hard/soft)
    if not parsed.preassigned_df.empty:
        for _, row in parsed.preassigned_df.iterrows():
            tid = str(row.get("TaskID", "")).strip()
            if not tid:
                continue
            if tid not in tasks:
                raise ValueError(f"Preassigned references unknown TaskID '{tid}'")
            st = row.get("StartDateTime")
            en = row.get("EndDateTime")
            if st is None or en is None or str(st) == "NaT" or str(en) == "NaT":
                raise ValueError(f"Preassigned row has invalid datetimes for TaskID '{tid}'")
            seg = segment_from_row(tid, row.get("ResourceIDs", ""), st, en, note="preassigned")

            # Alignment & horizon
            seg = segment_from_row(seg.task_id, ";".join(seg.resource_ids), _align_dt(seg.start, cfg.slot_minutes), _align_dt(seg.end, cfg.slot_minutes), note=seg.note)
            if seg.start < start or seg.end > end:
                raise ValueError(f"Preassigned segment outside horizon for TaskID '{tid}': {seg.start} -> {seg.end}")

            # Validate resources exist
            for rid in seg.resource_ids:
                if rid not in resources:
                    raise ValueError(f"Preassigned segment references unknown ResourceID '{rid}' (task {tid})")

            mode = str(row.get("Mode", "HARD")).strip().upper()
            if mode not in ("HARD", "SOFT"):
                raise ValueError(f"Preassigned Mode must be HARD or SOFT (task {tid})")

            # Basic sanity: preassigned resource count should match roles for that task.
            # Roles = fixed resources + sum(skill counts)
            roles_needed = len(tasks[tid].requirement.fixed_resources) + sum(sr.count for sr in tasks[tid].requirement.skill_requirements)
            if roles_needed != len(seg.resource_ids):
                raise ValueError(
                    f"Preassigned segment for task '{tid}' has {len(seg.resource_ids)} resources, but the task requires {roles_needed}."
                )

            if mode == "HARD":
                tasks[tid].preassigned_hard.append(seg)
            else:
                tasks[tid].preassigned_soft.append(seg)

    return Problem(
        start=start,
        end=end,
        slot_minutes=cfg.slot_minutes,
        resources=resources,
        tasks=tasks,
        availability_mask=availability_mask,
    )
