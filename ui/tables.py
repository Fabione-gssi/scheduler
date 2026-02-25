from __future__ import annotations

import pandas as pd


def render_segments_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    show = df[["TaskID", "Task", "Resources", "Start", "End", "DurationHours", "Priority", "Note"]].copy()
    return show
