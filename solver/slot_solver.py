from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Tuple

from ortools.sat.python import cp_model

from core.models import Overrides, Problem, Segment, Solution, SolveLimits, Weights, Window
from core.timegrid import TimeGrid
from .interface import Solver
from .metrics import compute_basic_metrics

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Role:
    """A role-unit required by a task whenever it is active."""
    name: str
    allowed_resources: tuple[str, ...]

def _build_roles(problem: Problem, task_id: str) -> list[Role]:
    t = problem.tasks[task_id]
    roles: list[Role] = []

    for rid in t.requirement.fixed_resources:
        roles.append(Role(name=f"fixed:{rid}", allowed_resources=(rid,)))

    for sr in t.requirement.skill_requirements:
        pool = [r.id for r in problem.resources.values() if sr.skill in r.skills]
        pool_t = tuple(sorted(pool))
        for k in range(sr.count):
            roles.append(Role(name=f"skill:{sr.skill}:{k+1}", allowed_resources=pool_t))

    # NEW: if nothing specified, assume 1 generic role = any resource
    if not roles:
        all_resources = tuple(sorted(problem.resources.keys()))
        roles.append(Role(name="any:1", allowed_resources=all_resources))

    return roles

class SlotModelSolver(Solver):
    """MVP CP-SAT solver using discrete time slots.

    Model summary:
      - z[t,s] : task t is active in slot s
      - a[t,i,r,s] : role i of task t assigned to resource r in slot s

    Constraints:
      - each task gets exact duration slots (sum z)
      - for each active slot, each role is filled by exactly 1 allowed resource
      - per resource & slot, at most one role is assigned (capacity)
      - availability windows forbid assignments
      - earliest start, max splits (segments), preassigned segments
      - dependencies with lag (can be negative to allow overlap):
            start(successor) >= end(predecessor) + lag_slots
    """

    def solve(
        self,
        problem: Problem,
        weights: Weights,
        overrides: Overrides | None = None,
        limits: SolveLimits | None = None,
    ) -> Solution:
        overrides = overrides or Overrides()
        limits = limits or SolveLimits()

        grid = TimeGrid(start=problem.start, end=problem.end, slot_minutes=problem.slot_minutes)
        S = grid.n_slots
        tasks = list(problem.tasks.keys())
        resources = list(problem.resources.keys())

        model = cp_model.CpModel()

        # z variables
        z: Dict[Tuple[str, int], cp_model.IntVar] = {}
        for t in tasks:
            for s in range(S):
                z[(t, s)] = model.NewBoolVar(f"z[{t},{s}]")

        # Segment start indicators for fragmentation and bounds
        seg_start: Dict[Tuple[str, int], cp_model.IntVar] = {}
        seg_end: Dict[Tuple[str, int], cp_model.IntVar] = {}
        for t in tasks:
            for s in range(S):
                seg_start[(t, s)] = model.NewBoolVar(f"seg_start[{t},{s}]")
                seg_end[(t, s)] = model.NewBoolVar(f"seg_end[{t},{s}]")

        for t in tasks:
            # seg_start[0] == z[0]
            model.Add(seg_start[(t, 0)] == z[(t, 0)])
            for s in range(1, S):
                # seg_start = z[s] AND NOT z[s-1]  (linearized)
                model.Add(seg_start[(t, s)] <= z[(t, s)])
                model.Add(seg_start[(t, s)] <= 1 - z[(t, s - 1)])
                model.Add(seg_start[(t, s)] >= z[(t, s)] - z[(t, s - 1)])

            # seg_end: z[s] AND (s==S-1 OR NOT z[s+1])
            for s in range(S - 1):
                model.Add(seg_end[(t, s)] <= z[(t, s)])
                model.Add(seg_end[(t, s)] <= 1 - z[(t, s + 1)])
                model.Add(seg_end[(t, s)] >= z[(t, s)] - z[(t, s + 1)])
            model.Add(seg_end[(t, S - 1)] == z[(t, S - 1)])

        # Task duration constraints
        for t in tasks:
            dur = problem.tasks[t].duration_slots
            model.Add(sum(z[(t, s)] for s in range(S)) == dur)

        # Earliest start constraints
        for t in tasks:
            earliest = problem.tasks[t].earliest_slot
            if earliest is not None:
                for s in range(0, min(earliest, S)):
                    model.Add(z[(t, s)] == 0)

        # Max splits constraints (segments count = sum seg_start)
        for t in tasks:
            task = problem.tasks[t]
            max_segments = task.max_splits + 1 if task.splittable else 1
            model.Add(sum(seg_start[(t, s)] for s in range(S)) <= max_segments)

        # Compute global start/end for each task (min first segment start, max last segment end)
        start_slot: Dict[str, cp_model.IntVar] = {}
        end_slot: Dict[str, cp_model.IntVar] = {}
        first: Dict[Tuple[str, int], cp_model.IntVar] = {}
        last: Dict[Tuple[str, int], cp_model.IntVar] = {}

        for t in tasks:
            start_slot[t] = model.NewIntVar(0, S - 1, f"start[{t}]")
            end_slot[t] = model.NewIntVar(1, S, f"end[{t}]")  # end is exclusive
            # choose exactly one first segment start among seg_start
            for s in range(S):
                first[(t, s)] = model.NewBoolVar(f"first[{t},{s}]")
                model.Add(first[(t, s)] <= seg_start[(t, s)])

            model.Add(sum(first[(t, s)] for s in range(S)) == 1)

            for s in range(S):
                # If first at s, then no segment starts before s
                if s > 0:
                    model.Add(sum(seg_start[(t, u)] for u in range(0, s)) == 0).OnlyEnforceIf(first[(t, s)])
                # bind start_slot
                model.Add(start_slot[t] == s).OnlyEnforceIf(first[(t, s)])

            # choose exactly one last segment end among seg_end
            for s in range(S):
                last[(t, s)] = model.NewBoolVar(f"last[{t},{s}]")
                model.Add(last[(t, s)] <= seg_end[(t, s)])

            model.Add(sum(last[(t, s)] for s in range(S)) == 1)

            for s in range(S):
                # If last at s, then no segment ends after s
                if s < S - 1:
                    model.Add(sum(seg_end[(t, u)] for u in range(s + 1, S)) == 0).OnlyEnforceIf(last[(t, s)])
                model.Add(end_slot[t] == (s + 1)).OnlyEnforceIf(last[(t, s)])

        # Roles and assignments a[t,i,r,s]
        roles_by_task: Dict[str, list[Role]] = {t: _build_roles(problem, t) for t in tasks}
        # Validate every task has at least 1 role

        # Assignment variables (sparse by allowed resources and availability)
        a: Dict[Tuple[str, int, str, int], cp_model.IntVar] = {}

        # Capacity helpers per resource & slot
        cap_terms: Dict[Tuple[str, int], list[cp_model.IntVar]] = {(r, s): [] for r in resources for s in range(S)}

        for t in tasks:
            for i, role in enumerate(roles_by_task[t]):
                for s in range(S):
                    # If task not active, role must be unassigned. We'll enforce sum_r a == z.
                    for r in role.allowed_resources:
                        if not problem.availability_mask[r][s]:
                            continue  # cannot assign when unavailable
                        v = model.NewBoolVar(f"a[{t},{i},{r},{s}]")
                        a[(t, i, r, s)] = v
                        cap_terms[(r, s)].append(v)

        # For each (t,i,s): sum_{r} a == z[t,s]
        for t in tasks:
            for i, role in enumerate(roles_by_task[t]):
                for s in range(S):
                    vars_here = [a[(t, i, r, s)] for r in role.allowed_resources if (t, i, r, s) in a]
                    # If no available resources in this slot, then z must be 0 for feasibility
                    if not vars_here:
                        model.Add(z[(t, s)] == 0)
                    else:
                        model.Add(sum(vars_here) == z[(t, s)])

        # Resource capacity per slot: sum assignments <= 1
        for r in resources:
            for s in range(S):
                terms = cap_terms[(r, s)]
                if terms:
                    model.Add(sum(terms) <= 1)

        # Apply hard preassignments (task fixed in given slots with given resources)
        def _enforce_segment_hard(task_id: str, resource_ids: tuple[str, ...], w: Window) -> None:
            s0, s1 = grid.window_to_slot_range(w.start, w.end)
            roles = roles_by_task[task_id]
            if len(resource_ids) != len(roles):
                raise ValueError(f"Hard segment mismatch for task {task_id}: resources != roles")
            for s in range(s0, s1):
                model.Add(z[(task_id, s)] == 1)
                # force each given resource to be used by exactly one role in this slot
                for rid in resource_ids:
                    vars_for_r = [a[(task_id, i, rid, s)] for i in range(len(roles)) if (task_id, i, rid, s) in a]
                    if not vars_for_r:
                        raise ValueError(f"Hard segment impossible: resource {rid} not assignable in slot {s}")
                    model.Add(sum(vars_for_r) == 1)

        for t in tasks:
            task = problem.tasks[t]
            for seg in task.preassigned_hard:
                _enforce_segment_hard(seg.task_id, seg.resource_ids, Window(seg.start, seg.end))

        # Apply task time windows (allowed mask)
        for t in tasks:
            allowed = problem.task_allowed_mask.get(t)
            if allowed is None:
                continue
            for s in range(S):
                if not allowed[s]:
                    model.Add(z[(t, s)] == 0)
        
        # Apply overrides: locks & bans (hard)
        for lock in overrides.locks:
            _enforce_segment_hard(lock.task_id, lock.resource_ids, lock.window)

        for ban in overrides.bans:
            s0, s1 = grid.window_to_slot_range(ban.window.start, ban.window.end)
            for s in range(s0, s1):
                # Forbid resource usage by this task in this slot across all roles
                roles = roles_by_task[ban.task_id]
                for i in range(len(roles)):
                    key = (ban.task_id, i, ban.resource_id, s)
                    if key in a:
                        model.Add(a[key] == 0)

        # Dependencies with lag (may be negative to allow overlap)
        # start[succ] >= end[pred] + lag
        for succ_id, succ in problem.tasks.items():
            for dep in succ.dependencies:
                pred_id = dep.predecessor_id
                model.Add(start_slot[succ_id] >= end_slot[pred_id] + dep.lag_slots)

        # Objective components
        objective_terms: list[cp_model.LinearExpr] = []

        # (1) Deadline tardiness
        if weights.w_deadline > 0:
            for t_id, task in problem.tasks.items():
                if task.due_slot is None:
                    continue
                tard = model.NewIntVar(0, S, f"tard[{t_id}]")
                # tard >= end - due ; tard >= 0
                model.Add(tard >= end_slot[t_id] - task.due_slot)
                model.Add(tard >= 0)
                objective_terms.append(weights.w_deadline * task.priority * tard)

        # (2) Fragmentation: number of segments (starts) per task
        if weights.w_fragmentation > 0:
            for t_id, task in problem.tasks.items():
                seg_count = model.NewIntVar(1, S, f"segcount[{t_id}]")
                model.Add(seg_count == sum(seg_start[(t_id, s)] for s in range(S)))
                objective_terms.append(weights.w_fragmentation * task.priority * seg_count)

        # (3) Switching / starts of busy blocks per resource (simple proxy)
        if weights.w_switching > 0:
            for r in resources:
                # busy[s] is 1 if any role uses resource r in slot s (capacity makes it 0/1)
                busy = [model.NewBoolVar(f"busy[{r},{s}]") for s in range(S)]
                for s in range(S):
                    terms = cap_terms[(r, s)]
                    if terms:
                        # busy == sum(terms)
                        model.Add(sum(terms) == busy[s])
                    else:
                        model.Add(busy[s] == 0)
                busy_start = [model.NewBoolVar(f"busy_start[{r},{s}]") for s in range(S)]
                model.Add(busy_start[0] == busy[0])
                for s in range(1, S):
                    model.Add(busy_start[s] <= busy[s])
                    model.Add(busy_start[s] <= 1 - busy[s - 1])
                    model.Add(busy_start[s] >= busy[s] - busy[s - 1])
                objective_terms.append(weights.w_switching * sum(busy_start))

        # Soft preassignments (reward matching specified resources in specified slots)
        # Implement as penalty for NOT matching.
        soft_penalties: list[cp_model.LinearExpr] = []
        SOFT_UNIT_PENALTY = 10  # scaled so weights still meaningful; adjust later if needed
        for t in tasks:
            task = problem.tasks[t]
            for seg in task.preassigned_soft:
                s0, s1 = grid.window_to_slot_range(seg.start, seg.end)
                roles = roles_by_task[t]
                for s in range(s0, s1):
                    # penalize if any specified resource is not used
                    for rid in seg.resource_ids:
                        vars_for_r = [a[(t, i, rid, s)] for i in range(len(roles)) if (t, i, rid, s) in a]
                        if not vars_for_r:
                            # if unavailable, it's impossible to match; keep it as a penalty by forcing z=0 earlier
                            continue
                        used = model.NewBoolVar(f"soft_used[{t},{rid},{s}]")
                        model.Add(sum(vars_for_r) == used)  # due to capacity, sum is 0/1
                        miss = model.NewBoolVar(f"soft_miss[{t},{rid},{s}]")
                        model.Add(miss == 1 - used)
                        soft_penalties.append(SOFT_UNIT_PENALTY * miss)

        if objective_terms or soft_penalties:
            model.Minimize(sum(objective_terms) + sum(soft_penalties))

        # Solve
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(limits.max_time_seconds)
        solver.parameters.num_search_workers = int(limits.num_search_workers)

        status = solver.Solve(model)
        status_name = solver.StatusName(status)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return Solution(
                segments=[],
                metrics={},
                status=status_name,
                objective_value=None,
                infeasible_reason="No feasible schedule found (try increasing horizon, using 60-min slots, or relaxing constraints).",
            )

        # Extract solution -> build segments by scanning z and collecting participating resources per slot
        segs: list[Segment] = []
        for t in tasks:
            # For each slot, determine resources assigned (across roles)
            roles = roles_by_task[t]
            active = [bool(solver.Value(z[(t, s)])) for s in range(S)]
            # build contiguous segments in time for this task
            s = 0
            while s < S:
                if not active[s]:
                    s += 1
                    continue
                s0 = s
                while s < S and active[s]:
                    s += 1
                s1 = s
                # For each slot in [s0,s1), resource set may change. To keep output readable,
                # we split further whenever participant set changes.
                cur_start = s0
                cur_res = None
                for ss in range(s0, s1):
                    res_here: list[str] = []
                    for i, role in enumerate(roles):
                        chosen = None
                        for r in role.allowed_resources:
                            key = (t, i, r, ss)
                            if key in a and solver.Value(a[key]) == 1:
                                chosen = r
                                break
                        if chosen is not None:
                            res_here.append(chosen)
                    res_tuple = tuple(sorted(res_here))
                    if cur_res is None:
                        cur_res = res_tuple
                    if res_tuple != cur_res:
                        # close previous
                        segs.append(Segment(
                            task_id=t,
                            resource_ids=cur_res,
                            start=grid.slot_start(cur_start),
                            end=grid.slot_start(ss),
                            note="auto"
                        ))
                        cur_start = ss
                        cur_res = res_tuple
                # close last
                if cur_res is not None:
                    segs.append(Segment(
                        task_id=t,
                        resource_ids=cur_res,
                        start=grid.slot_start(cur_start),
                        end=grid.slot_start(s1),
                        note="auto"
                    ))

        metrics = compute_basic_metrics(segs)
        return Solution(
            segments=segs,
            metrics=metrics,
            status=status_name,
            objective_value=float(solver.ObjectiveValue()) if objective_terms or soft_penalties else None,
        )
