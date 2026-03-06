from __future__ import annotations

import json
from textwrap import dedent

import pandas as pd
import streamlit as st

from core.excel_generator import generated_workbook_to_xlsx_bytes, workbook_from_agent_json

st.set_page_config(page_title="Genera Excel da linguaggio naturale", layout="wide")
st.title("🤖 Generatore Excel assistito da agente")

st.markdown(
    """
Questa pagina ti aiuta a creare un file Excel coerente col solver partendo da:
1. **contesto in linguaggio naturale**;
2. output strutturato dell'agente in **JSON**.

> Suggerimento pratico: usa il prompt pronto qui sotto con il tuo LLM preferito,
> poi incolla il JSON generato e scarica il file `.xlsx`.
"""
)

with st.expander("1) Inserisci contesto e vincoli in linguaggio naturale", expanded=True):
    project_context = st.text_area(
        "Descrivi progetto/task/risorse e vincoli",
        height=180,
        placeholder="Es: Ho 12 task di cablaggio e test QA, 4 risorse...",
    )

    template_payload = {
        "tasks": [
            {
                "TaskID": "T1",
                "Name": "Cablaggio quadro A",
                "DurationHours": 4,
                "Priority": 4,
                "DueDateTime": "2026-03-10 18:00",
                "EarliestStart": "2026-03-10 09:00",
                "Splittable": "Y",
                "MaxSplits": 2,
                "FixedResources": "",
                "SkillReq": "ELEC:1",
                "Dependencies": "",
            }
        ],
        "resources": [
            {"ResourceID": "R1", "Name": "Mario", "Skills": "ELEC;QA"},
            {"ResourceID": "R2", "Name": "Luca", "Skills": "ELEC"},
        ],
        "unavailability": [
            {
                "ResourceID": "R1",
                "StartDateTime": "2026-03-11 09:00",
                "EndDateTime": "2026-03-11 13:00",
                "Reason": "Visita medica",
            }
        ],
        "preassigned": [
            {
                "TaskID": "T1",
                "ResourceIDs": "R1",
                "StartDateTime": "2026-03-10 09:00",
                "EndDateTime": "2026-03-10 11:00",
                "Mode": "SOFT",
            }
        ],
        "taskwindows": [
            {
                "TaskID": "T1",
                "StartDateTime": "2026-03-10 09:00",
                "EndDateTime": "2026-03-10 18:00",
                "Mode": "MUST",
            }
        ],
    }

    prompt = dedent(
        f"""
        Sei un agente che deve generare JSON per un file Excel di scheduling.

        Regole:
        - Rispondi SOLO con JSON valido.
        - Top-level keys obbligatorie: tasks, resources.
        - Keys opzionali: unavailability, preassigned, taskwindows.
        - Colonne tasks: TaskID, Name, DurationHours, Priority, DueDateTime, EarliestStart, Splittable, MaxSplits, FixedResources, SkillReq, Dependencies.
        - Colonne resources: ResourceID, Name, Skills.
        - TaskWindows.Mode: BAN | MUST | NICE.
        - Preassigned.Mode: HARD | SOFT.
        - Liste multiple in stringa separate da ';' (es: Skills='ELEC;QA').
        - Dependencies formato: 'PRED:lag_hours;PRED2:lag_hours'.

        Contesto utente:
        {project_context or '[nessun contesto fornito]'}

        Esempio di struttura attesa:
        {json.dumps(template_payload, indent=2, ensure_ascii=False)}
        """
    ).strip()

    st.code(prompt, language="text")

with st.expander("2) Incolla qui il JSON prodotto dall'agente", expanded=True):
    json_input = st.text_area("JSON agente", height=300, placeholder='{"tasks": [...], "resources": [...]}')

    if st.button("Valida JSON e genera Excel", type="primary"):
        try:
            generated = workbook_from_agent_json(json_input)
            output_bytes = generated_workbook_to_xlsx_bytes(generated)
        except Exception as exc:
            st.error(f"Errore validazione/generazione: {exc}")
        else:
            st.success("Workbook generato correttamente ✅")

            c1, c2 = st.columns(2)
            with c1:
                st.subheader("Tasks preview")
                st.dataframe(generated.tasks_df.head(30), use_container_width=True)
            with c2:
                st.subheader("Resources preview")
                st.dataframe(generated.resources_df.head(30), use_container_width=True)

            st.download_button(
                "Scarica Excel input (.xlsx)",
                data=output_bytes,
                file_name="scheduler_input_generated.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

st.divider()
st.caption(
    "Nota: questa pagina non chiama automaticamente un provider LLM. "
    "Ti permette di standardizzare prompt, validare output JSON e produrre Excel coerente."
)
