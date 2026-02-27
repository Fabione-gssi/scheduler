from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time
from typing import Iterable

from .excel_io import (
    ParsedInput,
    parse_dependencies_cell,
    parse_list_cell,
    parse_skillreq_cell,
    segment_from_row,
)
from .models import Problem, Resource, Task, TaskRequirement, Window
from .timegrid import TimeGrid


@dataclass(frozen=True)
class BuildConfig:
    slot_minutes: int
    horizon_start: datetime
    horizon_end: datetime

    work_days: set[int] = frozenset({0, 1, 2, 3, 4})  # Mon..Fri

    # Working blocks per day (half-open): [start, end)
    # Default: 09-13 and 14-18 (lunch break 13-14)
    work_blocks: tuple[tuple[time, time], ...] = (
        (time(9, 0), time(13, 0)),
        (time(14, 0), time(18, 0)),
    )


def _align_dt(dt_in: datetime, slot_minutes: int) -> datetime:
    minute = (dt_in.minute // slot_minutes) * slot_minutes
    return dt_in.replace(minute=minute, second=0, microsecond=0)


def _dt_to_slot(grid: TimeGrid, dt_in: datetime) -> int:
    """Map an aligned datetime to a slot index (0..S)."""
    if dt_in < grid.start or dt_in > grid.end:
        raise ValueError(f"Datetime outside horizon: {dt_in}")
    delta = dt_in - grid.start
    return int(delta.total_seconds() // (grid.slot_minutes * 60))


def _common_work_slots(grid: TimeGrid, cfg: BuildConfig) -> list[bool]:
    """
    True for slots that fully fit inside one of the work_blocks
    (and on allowed weekdays).
    This enforces lunch breaks like 13-14 by simply not including that block.
    """
    allowed = [False] * grid.n_slots

    for s in range(grid.n_slots):
        st = grid.slot_start(s)
        en = st + grid.slot_delta

        if st.weekday() not in cfg.work_days:
            continue
        if st.date() != en.date():
            continue  # slot crosses midnight -> disallow

        st_t = st.time()
        en_t = en.time()

        ok = False
        for b_start, b_end in cfg.work_blocks:
            # slot must fit completely inside a work block
            if (b_start <= st_t) and (en_t <= b_end):
                ok = True
                break

        if ok:
            allowed[s] = True

    return allowed


def _apply_unavailability(mask: list[bool], grid: TimeGrid, windows: Iterable[Window]) -> list[bool]:
    out = mask[:]
    for w in windows:
        s0, s1 = grid.window_to_slot_range(w.start, w.end)
        for s in range(s0, s1):
            out[s] = False
    return out


def _add_window_to_mask(
    grid: TimeGrid,
    cfg: BuildConfig,
    start: datetime,
    end: datetime,
    task_id: str,
    st,
    en,
    label: str,
    mask: list[bool],
) -> None:
    if st is None or en is None or str(st) == "NaT" or str(en) == "NaT":
        raise ValueError(f"TaskWindows: datetime non valide per TaskID '{task_id}' ({label})")

    st = st.to_pydatetime() if hasattr(st, "to_pydatetime") else st
    en = en.to_pydatetime() if hasattr(en, "to_pydatetime") else en
    st = _align_dt(st, cfg.slot_minutes)
    en = _align_dt(en, cfg.slot_minutes)

    if en <= st:
        raise ValueError(f"TaskWindows: finestra vuota/invertita per TaskID '{task_id}' ({label}): {st} -> {en}")
    if st < start or en > end:
        raise ValueError(f"TaskWindows: finestra fuori orizzonte per TaskID '{task_id}' ({label}): {st} -> {en}")

    s0, s1 = grid.window_to_slot_range(st, en)
    for s in range(s0, s1):
        mask[s] = True


def build_problem(parsed: ParsedInput, cfg: BuildConfig) -> Problem:
    # Align horizon
    start = _align_dt(cfg.horizon_start, cfg.slot_minutes)
    end = _align_dt(cfg.horizon_end, cfg.slot_minutes)
    if end <= start:
        raise ValueError("Horizon end must be after start")

    grid = TimeGrid(start=start, end=end, slot_minutes=cfg.slot_minutes)
    S = grid.n_slots

    # -------------------------
    # Resources
    # -------------------------
    resources: dict[str, Resource] = {}
    for _, row in parsed.resources_df.iterrows():
        rid = str(row.get("ResourceID", "")).strip()
        if not rid:
            continue
        if rid in resources:
            raise ValueError(f"Duplicate ResourceID: {rid}")
        name = str(row.get("Name", rid)).strip() or rid
        skills = set(parse_list_cell(row.get("Skills")))
        resources[rid] = Resource(id=rid, name=name, skills=skills)

    if not resources:
        raise ValueError("No resources provided.")

    # -------------------------
    # Unavailability
    # -------------------------
    if parsed.unavailability_df is not None and not parsed.unavailability_df.empty:
        for i, row in parsed.unavailability_df.iterrows():
            rid = str(row.get("ResourceID", "")).strip()
            if not rid:
                continue
            if rid not in resources:
                raise ValueError(f"Unavailability references unknown ResourceID '{rid}' (row {i})")

            st = row.get("StartDateTime")
            en = row.get("EndDateTime")
            if st is None or en is None or str(st) == "NaT" or str(en) == "NaT":
                raise ValueError(f"Unavailability row {i} has invalid datetimes for ResourceID '{rid}'")

            st = st.to_pydatetime() if hasattr(st, "to_pydatetime") else st
            en = en.to_pydatetime() if hasattr(en, "to_pydatetime") else en
            st = _align_dt(st, cfg.slot_minutes)
            en = _align_dt(en, cfg.slot_minutes)

            if en <= st:
                raise ValueError(f"Unavailability row {i} has end <= start for ResourceID '{rid}': {st}->{en}")
            if st < start or en > end:
                raise ValueError(f"Unavailability window outside horizon for ResourceID '{rid}': {st} -> {en}")

            resources[rid].unavailability.append(Window(start=st, end=en))

    # Availability per resource: common work calendar minus unavailability
    common_mask = _common_work_slots(grid, cfg)
    availability_mask: dict[str, list[bool]] = {}
    for rid, res in resources.items():
        availability_mask[rid] = _apply_unavailability(common_mask, grid, res.unavailability)

    # -------------------------
    # Tasks
    # -------------------------
    tasks: dict[str, Task] = {}
    for _, row in parsed.tasks_df.iterrows():
        tid = str(row.get("TaskID", "")).strip()
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
            if max_splits < 0:
                max_splits = 0

        fixed_resources = tuple(parse_list_cell(row.get("FixedResources")))
        for rid in fixed_resources:
            if rid not in resources:
                raise ValueError(f"Task '{tid}' references unknown fixed resource '{rid}'")

        skill_reqs = tuple(parse_skillreq_cell(row.get("SkillReq")))
        for sr in skill_reqs:
            pool = [r for r in resources.values() if sr.skill in r.skills]
            if len(pool) < sr.count:
                raise ValueError(
                    f"Task '{tid}' requires {sr.count}x skill '{sr.skill}', but only {len(pool)} resources have it."
                )

        requirement = TaskRequirement(fixed_resources=fixed_resources, skill_requirements=skill_reqs)

        due_dt = row.get("DueDateTime")
        if due_dt is not None and str(due_dt) != "NaT":
            due_dt = due_dt.to_pydatetime() if hasattr(due_dt, "to_pydatetime") else due_dt
            due_dt = _align_dt(due_dt, cfg.slot_minutes)
            if not (start <= due_dt <= end):
                raise ValueError(f"Task '{tid}' DueDateTime is outside horizon: {due_dt}")
            due_slot = _dt_to_slot(grid, due_dt)
        else:
            due_slot = None

        earliest_dt = row.get("EarliestStart")
        if earliest_dt is not None and str(earliest_dt) != "NaT":
            earliest_dt = earliest_dt.to_pydatetime() if hasattr(earliest_dt, "to_pydatetime") else earliest_dt
            earliest_dt = _align_dt(earliest_dt, cfg.slot_minutes)
            if not (start <= earliest_dt <= end):
                raise ValueError(f"Task '{tid}' EarliestStart is outside horizon: {earliest_dt}")
            earliest_slot = _dt_to_slot(grid, earliest_dt)
        else:
            earliest_slot = None

        deps = parse_dependencies_cell(row.get("Dependencies"), cfg.slot_minutes)

        tasks[tid] = Task(
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

    if not tasks:
        raise ValueError("No tasks provided.")

    # Dependencies refer to existing tasks
    for t in tasks.values():
        for d in t.dependencies:
            if d.predecessor_id not in tasks:
                raise ValueError(f"Task '{t.id}' depends on unknown predecessor '{d.predecessor_id}'")

    # -------------------------
    # Preassigned segments (HARD/SOFT)
    # -------------------------
    if parsed.preassigned_df is not None and not parsed.preassigned_df.empty:
        for i, row in parsed.preassigned_df.iterrows():
            tid = str(row.get("TaskID", "")).strip()
            if not tid:
                continue
            if tid not in tasks:
                raise ValueError(f"Preassigned references unknown TaskID '{tid}' (row {i})")

            st = row.get("StartDateTime")
            en = row.get("EndDateTime")
            if st is None or en is None or str(st) == "NaT" or str(en) == "NaT":
                raise ValueError(f"Preassigned row {i} has invalid datetimes for TaskID '{tid}'")

            seg = segment_from_row(tid, row.get("ResourceIDs", ""), st, en, note="preassigned")

            seg = segment_from_row(
                seg.task_id,
                ";".join(seg.resource_ids),
                _align_dt(seg.start, cfg.slot_minutes),
                _align_dt(seg.end, cfg.slot_minutes),
                note=seg.note,
            )

            if seg.end <= seg.start:
                raise ValueError(f"Preassigned row {i} has end <= start for TaskID '{tid}'")
            if seg.start < start or seg.end > end:
                raise ValueError(f"Preassigned segment outside horizon for TaskID '{tid}': {seg.start} -> {seg.end}")

            for rid in seg.resource_ids:
                if rid not in resources:
                    raise ValueError(f"Preassigned segment references unknown ResourceID '{rid}' (task {tid}, row {i})")

            mode = str(row.get("Mode", "HARD")).strip().upper()
            if mode not in ("HARD", "SOFT"):
                raise ValueError(f"Preassigned Mode must be HARD or SOFT (task {tid}, row {i})")

            roles_needed = len(tasks[tid].requirement.fixed_resources) + sum(
                sr.count for sr in tasks[tid].requirement.skill_requirements
            )
            if roles_needed > 0 and roles_needed != len(seg.resource_ids):
                raise ValueError(
                    f"Preassigned segment for task '{tid}' has {len(seg.resource_ids)} resources, "
                    f"but the task requires {roles_needed}."
                )

            if mode == "HARD":
                tasks[tid].preassigned_hard.append(seg)
            else:
                tasks[tid].preassigned_soft.append(seg)

    # -------------------------
    # TaskWindows: BAN / MUST / NICE
    # -------------------------
    task_allowed_mask: dict[str, list[bool]] = {tid: [True] * S for tid in tasks.keys()}
    task_nice_mask: dict[str, list[bool]] = {tid: [False] * S for tid in tasks.keys()}

    tw = parsed.taskwindows_df
    if tw is not None and not tw.empty:
        # Validate TaskIDs exist
        for tid in tw.get("TaskID", []).dropna().astype(str).str.strip().tolist():
            if tid and tid not in tasks:
                raise ValueError(f"TaskWindows references unknown TaskID '{tid}'")

        # Normalize modes
        tw2 = tw.copy()
        tw2["TaskID"] = tw2["TaskID"].astype(str).str.strip()
        tw2["Mode"] = tw2.get("Mode", "").fillna("").astype(str).str.upper().str.strip()

        allowed_modes = {"BAN", "MUST", "NICE", ""}
        bad_modes = sorted(set(m for m in tw2["Mode"].unique() if m not in allowed_modes))
        if bad_modes:
            raise ValueError(f"TaskWindows: Mode non valido: {bad_modes}. Ammessi: BAN, MUST, NICE (o vuoto).")

        for tid, g in tw2.groupby("TaskID"):
            tid = str(tid).strip()
            if not tid:
                continue

            must_mask = [False] * S
            ban_mask = [False] * S
            nice_mask = [False] * S

            for _, row in g[g["Mode"] == "MUST"].iterrows():
                _add_window_to_mask(grid, cfg, start, end, tid, row["StartDateTime"], row["EndDateTime"], "MUST", must_mask)
            for _, row in g[g["Mode"] == "BAN"].iterrows():
                _add_window_to_mask(grid, cfg, start, end, tid, row["StartDateTime"], row["EndDateTime"], "BAN", ban_mask)
            for _, row in g[g["Mode"] == "NICE"].iterrows():
                _add_window_to_mask(grid, cfg, start, end, tid, row["StartDateTime"], row["EndDateTime"], "NICE", nice_mask)

            must_exists = any(must_mask)
            allowed = must_mask[:] if must_exists else [True] * S
            allowed = [a and (not b) for a, b in zip(allowed, ban_mask)]

            task_allowed_mask[tid] = allowed
            task_nice_mask[tid] = nice_mask

            # Fail-fast: MUST coverage check vs duration (time-only; resource availability is checked in solver/diagnosis)
            if must_exists:
                allowed_slots = sum(1 for v in allowed if v)
                if allowed_slots < tasks[tid].duration_slots:
                    raise ValueError(
                        f"Task '{tid}': finestre MUST consentono {allowed_slots} slot, "
                        f"ma la durata è {tasks[tid].duration_slots}. "
                        f"Aumenta MUST o riduci la durata."
                    )

    return Problem(
        start=start,
        end=end,
        slot_minutes=cfg.slot_minutes,
        resources=resources,
        tasks=tasks,
        availability_mask=availability_mask,
        task_allowed_mask=task_allowed_mask,
        task_nice_mask=task_nice_mask,
    )