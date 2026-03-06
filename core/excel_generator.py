from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any

import pandas as pd

REQUIRED_TOP_LEVEL_KEYS = ("tasks", "resources")


@dataclass(frozen=True)
class GeneratedWorkbook:
    tasks_df: pd.DataFrame
    resources_df: pd.DataFrame
    unavailability_df: pd.DataFrame
    preassigned_df: pd.DataFrame
    taskwindows_df: pd.DataFrame



def _normalize_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""



def _df_from_records(records: Any, columns: list[str]) -> pd.DataFrame:
    if records is None:
        return pd.DataFrame(columns=columns)
    if not isinstance(records, list):
        raise ValueError("Le sezioni JSON devono essere liste di oggetti.")
    out = pd.DataFrame(records)
    for c in columns:
        if c not in out.columns:
            out[c] = None
    return out[columns].copy()



def workbook_from_agent_json(json_text: str) -> GeneratedWorkbook:
    """Convert an LLM/agent JSON payload into normalized workbook dataframes.

    Expected schema (top-level):
      {
        "tasks": [...],
        "resources": [...],
        "unavailability": [...],
        "preassigned": [...],
        "taskwindows": [...]
      }
    """
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON non valido: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("Il JSON deve essere un oggetto con chiavi top-level.")

    for key in REQUIRED_TOP_LEVEL_KEYS:
        if key not in payload:
            raise ValueError(f"Chiave top-level mancante: '{key}'.")

    tasks_df = _df_from_records(
        payload.get("tasks"),
        [
            "TaskID",
            "Name",
            "DurationHours",
            "Priority",
            "DueDateTime",
            "EarliestStart",
            "Splittable",
            "MaxSplits",
            "FixedResources",
            "SkillReq",
            "Dependencies",
        ],
    )

    resources_df = _df_from_records(payload.get("resources"), ["ResourceID", "Name", "Skills"])
    unavailability_df = _df_from_records(
        payload.get("unavailability"), ["ResourceID", "StartDateTime", "EndDateTime", "Reason"]
    )
    preassigned_df = _df_from_records(
        payload.get("preassigned"), ["TaskID", "ResourceIDs", "StartDateTime", "EndDateTime", "Mode"]
    )
    taskwindows_df = _df_from_records(
        payload.get("taskwindows"), ["TaskID", "StartDateTime", "EndDateTime", "Mode"]
    )

    # Basic sanitation for core required entities
    for col in ("TaskID", "Name"):
        tasks_df[col] = tasks_df[col].map(_normalize_str)
    for col in ("ResourceID", "Name"):
        resources_df[col] = resources_df[col].map(_normalize_str)

    if tasks_df["TaskID"].eq("").any():
        raise ValueError("Ogni task deve avere TaskID valorizzato.")
    if resources_df["ResourceID"].eq("").any():
        raise ValueError("Ogni risorsa deve avere ResourceID valorizzato.")

    if tasks_df["TaskID"].duplicated().any():
        raise ValueError("TaskID duplicati nel JSON agente.")
    if resources_df["ResourceID"].duplicated().any():
        raise ValueError("ResourceID duplicati nel JSON agente.")

    return GeneratedWorkbook(
        tasks_df=tasks_df,
        resources_df=resources_df,
        unavailability_df=unavailability_df,
        preassigned_df=preassigned_df,
        taskwindows_df=taskwindows_df,
    )



def generated_workbook_to_xlsx_bytes(generated: GeneratedWorkbook) -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        generated.tasks_df.to_excel(writer, index=False, sheet_name="Tasks")
        generated.resources_df.to_excel(writer, index=False, sheet_name="Resources")
        generated.unavailability_df.to_excel(writer, index=False, sheet_name="Unavailability")
        generated.preassigned_df.to_excel(writer, index=False, sheet_name="Preassigned")
        generated.taskwindows_df.to_excel(writer, index=False, sheet_name="TaskWindows")
    out.seek(0)
    return out.read()
