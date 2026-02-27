# solver/slot_solver.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Tuple, List

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

    # Fixed resources -> fixed roles
    for rid in t.requirement.fixed_resources:
        roles.append(Role(name=f"fixed:{rid}", allowed_resources=(rid,)))

    # Skill requirements -> expand to count role-units
    for sr in t.requirement.skill_requirements:
        pool = [r.id for r in problem.resources.values() if sr.skill in r.skills]
        pool_t = tuple(sorted(pool))
        for k in range(sr.count):
            roles.append(Role(name=f"skill:{sr.skill}:{k+1}", allowed_resources=pool_t))

    # If nothing specified, assume 1 generic role = any resource
    if not roles:
        all_resources = tuple(sorted(problem.resources.keys()))
        roles.append(Role(name="any:1", allowed_resources=all_resources))

    return roles


def _diagnose_quick(problem: Problem) -> str:
    """
    Best-effort diagnosis for INFEASIBLE/UNKNOWN.
    Flags tasks that cannot reach duration given:
      - task_allowed_mask
      - at least one available resource per role per slot
    Also checks contiguity for non-splittable tasks.
    """
    grid = TimeGrid(start=problem.start, end=problem.end, slot_minutes=problem.slot_minutes)
    S = grid.n_slots

    roles_by_task: Dict[str, list[Role]] = {t: _build_roles(problem, t) for t in problem.tasks.keys()}

    bad: List[str] = []
    for tid, task in problem.tasks.items():
        allowed = problem.task_allowed_mask.get(tid, [True] * S)
        if len(allowed) != S:
            bad.append(f"{tid}: task_allowed_mask length mismatch ({len(allowed)} vs {S})")
            continue

        roles = roles_by_task[tid]

        feasible = [False] * S
        for s in range(S):
            if not allowed[s]:
                continue
            ok = True
            for role in roles:
                # must exist at least one resource in pool that is available in slot s
                if not any(problem.availability_mask[r][s] for r in role.allowed_resources):
                    ok = False
                    break
            feasible[s] = ok

        total = sum(1 for v in feasible if v)
        if total < task.duration_slots:
            bad.append(f"{tid}: feasible_slots={total} < duration={task.duration_slots}")
            continue

        if not task.splittable:
            best_run = 0
            cur = 0
            for s in range(S):
                if feasible[s]:
                    cur += 1
                    best_run = max(best_run, cur)
                else:
                    cur = 0
            if best_run < task.duration_slots:
                bad.append(f"{tid}: NON-SPLIT max_contiguous={best_run} < duration={task.duration_slots}")

    if not bad:
        return (
            "No obvious per-task feasibility blockers found. "
            "If status is UNKNOWN, it is likely a timeout / CPU contention."
        )
    return "Potential blockers (top 15): " + "; ".join(bad[:15])


class SlotModelSolver(Solver):
    """CP-SAT solver using discrete time slots.

    Variables:
      - z[t,s] : task t is active in slot s
      - a[t,i,r,s] : role i of task t assigned to resource r in slot s

    Key robustness features:
      - task_allowed_mask enforced once (hard)
      - start/end computed with AddMinEquality/AddMaxEquality (O(T·S), avoids O(T·S^2))
      - two-phase solve:
          1) feasibility only (no objective)
          2) optimization (if objective exists)
        If phase 2 returns UNKNOWN, we keep the feasible solution from phase 1.
      - status UNKNOWN is not treated as infeasible (it often means timeout)
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

        # -------------------------
        # z variables (task active)
        # -------------------------
        z: Dict[Tuple[str, int], cp_model.IntVar] = {}
        for t in tasks:
            for s in range(S):
                z[(t, s)] = model.NewBoolVar(f"z[{t},{s}]")

        # Apply task allowed mask (hard) ONCE
        for t in tasks:
            allowed = problem.task_allowed_mask.get(t)
            if allowed is None:
                continue
            if len(allowed) != S:
                raise ValueError(f"task_allowed_mask[{t}] length mismatch: {len(allowed)} vs {S}")
            for s in range(S):
                if not allowed[s]:
                    model.Add(z[(t, s)] == 0)

        # -------------------------
        # Fragmentation helpers: seg_start
        # -------------------------
        seg_start: Dict[Tuple[str, int], cp_model.IntVar] = {}
        for t in tasks:
            for s in range(S):
                seg_start[(t, s)] = model.NewBoolVar(f"seg_start[{t},{s}]")

        for t in tasks:
            model.Add(seg_start[(t, 0)] == z[(t, 0)])
            for s in range(1, S):
                model.Add(seg_start[(t, s)] <= z[(t, s)])
                model.Add(seg_start[(t, s)] <= 1 - z[(t, s - 1)])
                model.Add(seg_start[(t, s)] >= z[(t, s)] - z[(t, s - 1)])

        # -------------------------
        # Duration, earliest, splits
        # -------------------------
        for t in tasks:
            dur = problem.tasks[t].duration_slots
            model.Add(sum(z[(t, s)] for s in range(S)) == dur)

        for t in tasks:
            earliest = problem.tasks[t].earliest_slot
            if earliest is not None:
                for s in range(0, min(earliest, S)):
                    model.Add(z[(t, s)] == 0)

        for t in tasks:
            task = problem.tasks[t]
            max_segments = (task.max_splits + 1) if task.splittable else 1
            model.Add(sum(seg_start[(t, s)] for s in range(S)) <= max_segments)

        # -------------------------
        # Start/end per task (FAST, avoids O(S^2))
        # -------------------------
        start_slot: Dict[str, cp_model.IntVar] = {}
        end_slot: Dict[str, cp_model.IntVar] = {}
        BIG_M = S  # sufficiently large for min trick

        for t in tasks:
            start_slot[t] = model.NewIntVar(0, S - 1, f"start[{t}]")
            end_slot[t] = model.NewIntVar(1, S, f"end[{t}]")  # exclusive

            # start = min{s | z[t,s]=1} using candidates = s + M*(1-z)
            start_candidates = [model.NewIntVar(0, 2 * S, f"startCand[{t},{s}]") for s in range(S)]
            for s in range(S):
                model.Add(start_candidates[s] == s + BIG_M * (1 - z[(t, s)]))
            model.AddMinEquality(start_slot[t], start_candidates)

            # end = max{s+1 | z[t,s]=1} using candidates = (s+1)*z
            end_candidates = [model.NewIntVar(0, S, f"endCand[{t},{s}]") for s in range(S)]
            for s in range(S):
                model.Add(end_candidates[s] == (s + 1) * z[(t, s)])
            model.AddMaxEquality(end_slot[t], end_candidates)

        # -------------------------
        # Roles & assignments
        # -------------------------
        roles_by_task: Dict[str, list[Role]] = {t: _build_roles(problem, t) for t in tasks}

        a: Dict[Tuple[str, int, str, int], cp_model.IntVar] = {}
        cap_terms: Dict[Tuple[str, int], list[cp_model.IntVar]] = {(r, s): [] for r in resources for s in range(S)}

        # Create assignment vars only where availability is True
        for t in tasks:
            for i, role in enumerate(roles_by_task[t]):
                for s in range(S):
                    for r in role.allowed_resources:
                        if not problem.availability_mask[r][s]:
                            continue
                        v = model.NewBoolVar(f"a[{t},{i},{r},{s}]")
                        a[(t, i, r, s)] = v
                        cap_terms[(r, s)].append(v)

        # For each (t,i,s): sum_r a == z[t,s]
        for t in tasks:
            for i, role in enumerate(roles_by_task[t]):
                for s in range(S):
                    vars_here = [a[(t, i, r, s)] for r in role.allowed_resources if (t, i, r, s) in a]
                    if not vars_here:
                        model.Add(z[(t, s)] == 0)
                    else:
                        model.Add(sum(vars_here) == z[(t, s)])

        # Resource capacity per slot
        for r in resources:
            for s in range(S):
                terms = cap_terms[(r, s)]
                if terms:
                    model.Add(sum(terms) <= 1)

        # -------------------------
        # Hard preassignments & overrides
        # -------------------------
        def _enforce_segment_hard(task_id: str, resource_ids: tuple[str, ...], w: Window) -> None:
            s0, s1 = grid.window_to_slot_range(w.start, w.end)
            roles = roles_by_task[task_id]
            if len(resource_ids) != len(roles):
                raise ValueError(
                    f"Hard segment mismatch for task {task_id}: "
                    f"resources({len(resource_ids)}) != roles({len(roles)})"
                )
            for s in range(s0, s1):
                model.Add(z[(task_id, s)] == 1)
                for rid in resource_ids:
                    vars_for_r = [a[(task_id, i, rid, s)] for i in range(len(roles)) if (task_id, i, rid, s) in a]
                    if not vars_for_r:
                        raise ValueError(
                            f"Hard segment impossible: task {task_id}, resource {rid} not assignable in slot {s}"
                        )
                    model.Add(sum(vars_for_r) == 1)

        # Task-level hard preassigned
        for t in tasks:
            for seg in problem.tasks[t].preassigned_hard:
                _enforce_segment_hard(seg.task_id, seg.resource_ids, Window(seg.start, seg.end))

        # Overrides
        for lock in overrides.locks:
            _enforce_segment_hard(lock.task_id, lock.resource_ids, lock.window)

        for ban in overrides.bans:
            s0, s1 = grid.window_to_slot_range(ban.window.start, ban.window.end)
            roles = roles_by_task[ban.task_id]
            for s in range(s0, s1):
                for i in range(len(roles)):
                    key = (ban.task_id, i, ban.resource_id, s)
                    if key in a:
                        model.Add(a[key] == 0)

        # -------------------------
        # Dependencies with lag (may be negative)
        # -------------------------
        for succ_id, succ in problem.tasks.items():
            for dep in succ.dependencies:
                pred_id = dep.predecessor_id
                model.Add(start_slot[succ_id] >= end_slot[pred_id] + dep.lag_slots)

        # -------------------------
        # Objective expression (DON'T set it yet: used in phase 2)
        # -------------------------
        objective_terms: list[cp_model.LinearExpr] = []

        # (1) Deadline tardiness
        if weights.w_deadline > 0:
            for t_id, task in problem.tasks.items():
                if task.due_slot is None:
                    continue
                tard = model.NewIntVar(0, S, f"tard[{t_id}]")
                model.Add(tard >= end_slot[t_id] - task.due_slot)
                model.Add(tard >= 0)
                objective_terms.append(weights.w_deadline * task.priority * tard)

        # (2) Fragmentation (segments count)
        if weights.w_fragmentation > 0:
            for t_id, task in problem.tasks.items():
                seg_count = model.NewIntVar(1, S, f"segcount[{t_id}]")
                model.Add(seg_count == sum(seg_start[(t_id, s)] for s in range(S)))
                objective_terms.append(weights.w_fragmentation * task.priority * seg_count)

        # (3) NICE preference: apply only if task has at least one NICE slot
        if weights.w_nice > 0:
            for t_id, task in problem.tasks.items():
                nice = problem.task_nice_mask.get(t_id)
                if nice is None or len(nice) != S:
                    continue
                if not any(nice):
                    continue
                for s in range(S):
                    if nice[s]:
                        continue
                    objective_terms.append(weights.w_nice * task.priority * z[(t_id, s)])

        # Soft preassignments (penalty for NOT matching)
        soft_penalties: list[cp_model.LinearExpr] = []
        SOFT_UNIT_PENALTY = 10

        for t in tasks:
            for seg in problem.tasks[t].preassigned_soft:
                s0, s1 = grid.window_to_slot_range(seg.start, seg.end)
                roles = roles_by_task[t]
                for s in range(s0, s1):
                    for rid in seg.resource_ids:
                        vars_for_r = [a[(t, i, rid, s)] for i in range(len(roles)) if (t, i, rid, s) in a]
                        if not vars_for_r:
                            continue
                        used = model.NewBoolVar(f"soft_used[{t},{rid},{s}]")
                        model.Add(sum(vars_for_r) == used)
                        miss = model.NewBoolVar(f"soft_miss[{t},{rid},{s}]")
                        model.Add(miss == 1 - used)
                        soft_penalties.append(SOFT_UNIT_PENALTY * miss)

        objective_expr = None
        if objective_terms or soft_penalties:
            objective_expr = sum(objective_terms) + sum(soft_penalties)

        # -------------------------
        # Solve in 2 phases
        # -------------------------
        def _make_solver(time_s: float) -> cp_model.CpSolver:
            s = cp_model.CpSolver()
            # stability
            s.parameters.random_seed = 0
            s.parameters.num_search_workers = 1
            s.parameters.max_time_in_seconds = float(time_s)
            return s

        # Phase 1: Feasibility only (no objective)
        solver1 = _make_solver(time_s=float(max(limits.max_time_seconds, 30)))
        status1 = solver1.Solve(model)
        status1_name = solver1.StatusName(status1)

        if status1 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            reason = ""
            if status1 == cp_model.INFEASIBLE:
                reason = "Model is INFEASIBLE. " + _diagnose_quick(problem)
            elif status1 == cp_model.UNKNOWN:
                reason = (
                    "Solver returned UNKNOWN in feasibility phase (often timeout/CPU contention). "
                    "Try increasing max_time_seconds. "
                    + _diagnose_quick(problem)
                )
            else:
                reason = f"Solver status: {status1_name}. " + _diagnose_quick(problem)

            return Solution(
                segments=[],
                metrics={},
                status=status1_name,
                objective_value=None,
                infeasible_reason=reason,
            )

        # If no objective, keep phase-1 solution
        solver_used = solver1
        status_used = status1
        status_used_name = status1_name

        # Phase 2: Optimization (optional)
        if objective_expr is not None:
            model.Minimize(objective_expr)
            solver2 = _make_solver(time_s=float(max(limits.max_time_seconds, 60)))
            status2 = solver2.Solve(model)
            status2_name = solver2.StatusName(status2)

            # If optimization fails/unknown, keep feasible from phase 1
            if status2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                solver_used = solver2
                status_used = status2
                status_used_name = status2_name

        # -------------------------
        # Extract solution -> segments
        # -------------------------
        if status_used not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            # shouldn't happen, but keep it safe
            return Solution(
                segments=[],
                metrics={},
                status=status_used_name,
                objective_value=None,
                infeasible_reason="Unexpected solver state after solve phases. " + _diagnose_quick(problem),
            )

        segs: list[Segment] = []
        for t in tasks:
            roles = roles_by_task[t]
            active = [bool(solver_used.Value(z[(t, s)])) for s in range(S)]

            s = 0
            while s < S:
                if not active[s]:
                    s += 1
                    continue
                s0 = s
                while s < S and active[s]:
                    s += 1
                s1 = s

                # Split further when participant set changes
                cur_start = s0
                cur_res: tuple[str, ...] | None = None

                for ss in range(s0, s1):
                    res_here: list[str] = []
                    for i, role in enumerate(roles):
                        chosen = None
                        for r in role.allowed_resources:
                            key = (t, i, r, ss)
                            if key in a and solver_used.Value(a[key]) == 1:
                                chosen = r
                                break
                        if chosen is not None:
                            res_here.append(chosen)

                    res_tuple = tuple(sorted(res_here))
                    if cur_res is None:
                        cur_res = res_tuple

                    if res_tuple != cur_res:
                        segs.append(
                            Segment(
                                task_id=t,
                                resource_ids=cur_res,
                                start=grid.slot_start(cur_start),
                                end=grid.slot_start(ss),
                                note="auto",
                            )
                        )
                        cur_start = ss
                        cur_res = res_tuple

                if cur_res is not None:
                    segs.append(
                        Segment(
                            task_id=t,
                            resource_ids=cur_res,
                            start=grid.slot_start(cur_start),
                            end=grid.slot_start(s1),
                            note="auto",
                        )
                    )

        metrics = compute_basic_metrics(segs)

        # OR-Tools objective value is only meaningful if model has objective set (phase 2 used)
        obj_val = None
        if objective_expr is not None and solver_used is not solver1:
            try:
                obj_val = float(solver_used.ObjectiveValue())
            except Exception:
                obj_val = None

        return Solution(
            segments=segs,
            metrics=metrics,
            status=status_used_name,
            objective_value=obj_val,
        )