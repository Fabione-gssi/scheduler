from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from .models import Dependency, Segment, SkillRequirement


@dataclass(frozen=True)
class ParsedInput:
    tasks_df: pd.DataFrame
    resources_df: pd.DataFrame
    unavailability_df: pd.DataFrame
    preassigned_df: pd.DataFrame


REQUIRED_SHEETS = ["Tasks", "Resources"]


def _require_columns(df: pd.DataFrame, sheet: str, cols: list[str]) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"Sheet '{sheet}' is missing required columns: {missing}")


def parse_excel(path_or_bytes: Any) -> ParsedInput:
    """Parse the workbook into normalized DataFrames.

    This performs structural checks and basic type normalization.
    Semantic validation is performed later in core.problem_builder.
    """
    xl = pd.ExcelFile(path_or_bytes)

    for s in REQUIRED_SHEETS:
        if s not in xl.sheet_names:
            raise ValueError(f"Missing required sheet '{s}'. Found: {xl.sheet_names}")

    tasks_df = xl.parse("Tasks")
    resources_df = xl.parse("Resources")

    _require_columns(tasks_df, "Tasks", ["TaskID", "Name", "DurationHours", "Priority"])
    _require_columns(resources_df, "Resources", ["ResourceID", "Name"])

    unavailability_df = xl.parse("Unavailability") if "Unavailability" in xl.sheet_names else pd.DataFrame(
        columns=["ResourceID", "StartDateTime", "EndDateTime", "Reason"]
    )
    if "Unavailability" in xl.sheet_names:
        _require_columns(unavailability_df, "Unavailability", ["ResourceID", "StartDateTime", "EndDateTime"])
        if "Reason" not in unavailability_df.columns:
            unavailability_df["Reason"] = ""

    preassigned_df = xl.parse("Preassigned") if "Preassigned" in xl.sheet_names else pd.DataFrame(
        columns=["TaskID", "ResourceIDs", "StartDateTime", "EndDateTime", "Mode"]
    )
    if "Preassigned" in xl.sheet_names:
        _require_columns(preassigned_df, "Preassigned", ["TaskID", "ResourceIDs", "StartDateTime", "EndDateTime"])
        if "Mode" not in preassigned_df.columns:
            preassigned_df["Mode"] = "HARD"

    # Normalize types
    tasks_df = tasks_df.copy()
    resources_df = resources_df.copy()
    unavailability_df = unavailability_df.copy()
    preassigned_df = preassigned_df.copy()

    for col in ["TaskID", "Name"]:
        tasks_df[col] = tasks_df[col].astype(str).str.strip()
    tasks_df["Priority"] = pd.to_numeric(tasks_df["Priority"], errors="coerce").fillna(3).astype(int)
    tasks_df["DurationHours"] = pd.to_numeric(tasks_df["DurationHours"], errors="coerce")

    for col in ["ResourceID", "Name"]:
        resources_df[col] = resources_df[col].astype(str).str.strip()

    # Optional columns for tasks
    for col in ["DueDateTime", "EarliestStart", "Splittable", "MaxSplits", "FixedResources", "SkillReq", "Dependencies"]:
        if col not in tasks_df.columns:
            tasks_df[col] = None

    tasks_df["DueDateTime"] = pd.to_datetime(tasks_df["DueDateTime"], errors="coerce")
    tasks_df["EarliestStart"] = pd.to_datetime(tasks_df["EarliestStart"], errors="coerce")

    if not unavailability_df.empty:
        unavailability_df["StartDateTime"] = pd.to_datetime(unavailability_df["StartDateTime"], errors="coerce")
        unavailability_df["EndDateTime"] = pd.to_datetime(unavailability_df["EndDateTime"], errors="coerce")
        unavailability_df["Reason"] = unavailability_df["Reason"].astype(str).fillna("")

    if not preassigned_df.empty:
        preassigned_df["StartDateTime"] = pd.to_datetime(preassigned_df["StartDateTime"], errors="coerce")
        preassigned_df["EndDateTime"] = pd.to_datetime(preassigned_df["EndDateTime"], errors="coerce")
        preassigned_df["Mode"] = preassigned_df.get("Mode", "HARD").astype(str).str.upper().str.strip()

    return ParsedInput(tasks_df=tasks_df, resources_df=resources_df, unavailability_df=unavailability_df, preassigned_df=preassigned_df)


# ---------- helpers used by builder ----------

def parse_list_cell(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, float) and math.isnan(value):
        return []
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return []
    return [x.strip() for x in s.split(";") if x.strip()]


def parse_skillreq_cell(value: Any) -> list[SkillRequirement]:
    items = parse_list_cell(value)
    reqs: list[SkillRequirement] = []
    for it in items:
        if ":" not in it:
            raise ValueError(f"Invalid SkillReq item '{it}'. Expected SKILL:COUNT")
        skill, cnt = it.split(":", 1)
        skill = skill.strip()
        cnt_i = int(str(cnt).strip())
        reqs.append(SkillRequirement(skill=skill, count=cnt_i))
    return reqs


def parse_dependencies_cell(value: Any, slot_minutes: int) -> list[Dependency]:
    """Format: 'PRED1:lag_hours;PRED2:lag_hours'.

    lag_hours can be negative to allow overlap.
    Semantics: start(task) >= end(pred) + lag_slots.
    """
    items = parse_list_cell(value)
    deps: list[Dependency] = []
    for it in items:
        if ":" not in it:
            deps.append(Dependency(predecessor_id=it.strip(), lag_slots=0))
            continue
        pred, lag = it.split(":", 1)
        pred = pred.strip()
        lag_h = float(str(lag).strip())
        lag_slots = int(round((lag_h * 60) / slot_minutes))
        deps.append(Dependency(predecessor_id=pred, lag_slots=lag_slots))
    return deps


def segment_from_row(task_id: str, resource_ids_str: Any, start_dt: Any, end_dt: Any, note: str = "") -> Segment:
    rids = tuple([r.strip() for r in str(resource_ids_str).split(";") if r.strip()])
    if hasattr(start_dt, "to_pydatetime"):
        start_dt = start_dt.to_pydatetime()
    if hasattr(end_dt, "to_pydatetime"):
        end_dt = end_dt.to_pydatetime()
    return Segment(task_id=str(task_id).strip(), resource_ids=rids, start=start_dt, end=end_dt, note=note)
