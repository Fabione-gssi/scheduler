from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from core.excel_io import parse_excel
from core.problem_builder import BuildConfig, build_problem
from core.models import Overrides, Weights, SolveLimits
from solver.slot_solver import SlotModelSolver
from ui.gantt import gantt_figure, segments_to_df
from ui.tables import render_segments_table

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(page_title="Scheduler MVP", layout="wide")


def _default_horizon() -> tuple[datetime, datetime]:
    # default: next Monday 09:00 to Friday 18:00
    today = datetime.now().replace(second=0, microsecond=0)
    # find next Monday
    days_ahead = (0 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    start = (today + timedelta(days=days_ahead)).replace(hour=9, minute=0)
    end = (start + timedelta(days=5)).replace(hour=18, minute=0)
    return start, end


st.title("Calendario attività – MVP (Streamlit + OR-Tools)")

with st.sidebar:
    st.header("Input")
    uploaded = st.file_uploader("Carica Excel", type=["xlsx"])
    slot_minutes = st.selectbox("Slot (min)", options=[60, 30], index=0)

    st.header("Orizzonte")
    def_start, def_end = _default_horizon()
    horizon_start = st.datetime_input("Inizio", value=def_start)
    horizon_end = st.datetime_input("Fine", value=def_end)

    st.header("Ottimalità (3 pesi)")
    w_deadline = st.slider("Scadenze (tardiness)", 0, 100, 50, 1)
    w_fragmentation = st.slider("Frammentazione task", 0, 100, 30, 1)
    w_switching = st.slider("Switching risorse (proxy)", 0, 100, 20, 1)
    w_nice = st.slider("Preferenze (NICE)", 0, 100, 20, 1)

    st.header("Limiti solver")
    max_time = st.slider("Tempo massimo (s)", 5, 60, 20, 1)
    workers = st.selectbox("Search workers", options=[1, 2, 4, 8, 12, 16], index=3)

    run_btn = st.button("Genera calendario", type="primary", use_container_width=True)

if not uploaded:
    st.info("Carica un file Excel con fogli 'Tasks' e 'Resources'. (Template in /data/template.xlsx nel progetto)")
    st.stop()

try:
    parsed = parse_excel(uploaded)
except Exception as e:
    st.error(f"Errore parsing Excel: {e}")
    st.stop()

# Show quick preview
col1, col2 = st.columns(2)
with col1:
    st.subheader("Tasks (preview)")
    st.dataframe(parsed.tasks_df.head(20), use_container_width=True)
with col2:
    st.subheader("Resources (preview)")
    st.dataframe(parsed.resources_df.head(20), use_container_width=True)

if not run_btn:
    st.stop()

try:
    cfg = BuildConfig(
        slot_minutes=int(slot_minutes),
        horizon_start=horizon_start,
        horizon_end=horizon_end,
    )
    problem = build_problem(parsed, cfg)
except Exception as e:
    st.error(f"Errore validazione/build: {e}")
    st.stop()

weights = Weights(w_deadline=w_deadline, w_fragmentation=w_fragmentation, w_nice=w_nice)
limits = SolveLimits(max_time_seconds=int(max_time), num_search_workers=int(workers))

solver = SlotModelSolver()

# --- Overrides state ---
if "overrides" not in st.session_state:
    st.session_state["overrides"] = Overrides()

with st.expander("Modifiche (Lock / Ban) e ri-ottimizzazione", expanded=False):
    st.write("Lock = vincolo duro: task con esattamente queste risorse in questa finestra.")
    st.write("Ban = vincolo duro: task NON può usare questa risorsa in questa finestra.")

    colA, colB = st.columns(2)

    with colA:
        st.subheader("Aggiungi LOCK")
        lock_task = st.selectbox("Task", options=list(problem.tasks.keys()), key="lock_task")
        roles_needed = len(problem.tasks[lock_task].requirement.fixed_resources) + sum(
            sr.count for sr in problem.tasks[lock_task].requirement.skill_requirements
        )
        lock_resources = st.multiselect(
            f"Risorse (devono essere {roles_needed})",
            options=list(problem.resources.keys()),
            key="lock_res",
        )
        lock_start = st.datetime_input("Inizio lock", value=problem.start, key="lock_start")
        lock_end = st.datetime_input("Fine lock", value=problem.start + timedelta(hours=1), key="lock_end")

        if st.button("Aggiungi lock"):
            if len(lock_resources) != roles_needed:
                st.error(f"Numero risorse errato: servono {roles_needed}.")
            else:
                st.session_state["overrides"].locks.append(
                    LockOverride(
                        task_id=lock_task,
                        resource_ids=tuple(lock_resources),
                        window=Window(lock_start, lock_end),
                    )
                )
                st.success("Lock aggiunto.")

    with colB:
        st.subheader("Aggiungi BAN")
        ban_task = st.selectbox("Task", options=list(problem.tasks.keys()), key="ban_task")
        ban_resource = st.selectbox("Risorsa", options=list(problem.resources.keys()), key="ban_res")
        ban_start = st.datetime_input("Inizio ban", value=problem.start, key="ban_start")
        ban_end = st.datetime_input("Fine ban", value=problem.start + timedelta(hours=1), key="ban_end")

        if st.button("Aggiungi ban"):
            st.session_state["overrides"].bans.append(
                BanOverride(
                    task_id=ban_task,
                    resource_id=ban_resource,
                    window=Window(ban_start, ban_end),
                )
            )
            st.success("Ban aggiunto.")

    st.subheader("Overrides correnti")
    st.json({
        "locks": [dict(task=o.task_id, resources=o.resource_ids, start=str(o.window.start), end=str(o.window.end)) for o in st.session_state["overrides"].locks],
        "bans": [dict(task=o.task_id, resource=o.resource_id, start=str(o.window.start), end=str(o.window.end)) for o in st.session_state["overrides"].bans],
    })

    if st.button("Reset overrides"):
        st.session_state["overrides"] = Overrides()
        st.success("Overrides resettati.")

    rerun = st.button("Ricalcola con overrides", type="primary")

overrides = st.session_state["overrides"]
solution = solver.solve(problem, weights=weights, overrides=overrides, limits=limits)

if solution.status not in ("OPTIMAL", "FEASIBLE"):
    st.error(f"Solver status: {solution.status}\n\n{solution.infeasible_reason}")
    st.stop()

st.success(f"Solver status: {solution.status}" + (f" | Objective: {solution.objective_value:.2f}" if solution.objective_value is not None else ""))

# Metrics
with st.expander("Metriche", expanded=True):
    st.json(solution.metrics)

df = segments_to_df(problem, solution.segments)

# Table + Gantt
tab1, tab2 = st.tabs(["Tabella", "Gantt"])
with tab1:
    st.dataframe(render_segments_table(df), use_container_width=True, height=500)
with tab2:
    fig = gantt_figure(df)
    if fig is None:
        st.info("Nessun segmento da mostrare")
    else:
        st.plotly_chart(fig, use_container_width=True)

# Export schedule
out = io.BytesIO()
with pd.ExcelWriter(out, engine="openpyxl") as writer:
    df.to_excel(writer, index=False, sheet_name="Schedule")
    pd.DataFrame([solution.metrics]).to_excel(writer, index=False, sheet_name="Metrics")
out.seek(0)
st.download_button("Scarica output Excel", data=out, file_name="schedule_output.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
