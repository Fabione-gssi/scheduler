from __future__ import annotations

import pandas as pd
import plotly.express as px

from core.models import Problem, Segment


def segments_to_df(problem: Problem, segments: list[Segment]) -> pd.DataFrame:
    rows = []
    for seg in segments:
        rows.append({
            "TaskID": seg.task_id,
            "Task": problem.tasks[seg.task_id].name,
            "Resources": ", ".join(seg.resource_ids),
            "Start": seg.start,
            "End": seg.end,
            "DurationHours": (seg.end - seg.start).total_seconds() / 3600.0,
            "Priority": problem.tasks[seg.task_id].priority,
            "Note": seg.note,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["Resources", "Start", "TaskID"]).reset_index(drop=True)
    return df


def gantt_figure(df: pd.DataFrame):
    if df.empty:
        return None
    fig = px.timeline(
        df,
        x_start="Start",
        x_end="End",
        y="Resources",
        hover_data=["Task", "TaskID", "Priority", "DurationHours", "Note"],
        text="TaskID",
    )
    fig.update_yaxes(autorange="reversed")
    fig.update_layout(margin=dict(l=10, r=10, t=20, b=10), height=600)
    return fig
