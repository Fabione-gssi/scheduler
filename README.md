# Scheduler MVP (Streamlit + OR-Tools)

## Run locally
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Excel format (minimum)
Required sheets:
- Tasks: TaskID, Name, DurationHours, Priority
- Resources: ResourceID, Name

Optional sheets:
- Unavailability: ResourceID, StartDateTime, EndDateTime, Reason
- Preassigned: TaskID, ResourceIDs (semicolon-separated), StartDateTime, EndDateTime, Mode (HARD/SOFT)
- TaskWindows: TaskID, StartDateTime, EndDateTime, Mode (BAN/MUST/NICE)

## Excel format (full)

### Sheet: `Tasks`
Required columns:
- `TaskID` (string, unique)
- `Name` (string)
- `DurationHours` (number, > 0)
- `Priority` (integer 1..5)

Optional columns:
- `DueDateTime` (datetime)
- `EarliestStart` (datetime)
- `Splittable` (`Y/N`, `TRUE/FALSE`, `1/0`; default splittable)
- `MaxSplits` (integer, default 4 if splittable, else 0)
- `FixedResources` (semicolon-separated ResourceIDs, e.g. `R1;R2`)
- `SkillReq` (semicolon-separated `SKILL:COUNT`, e.g. `ELEC:1;QA:1`)
- `Dependencies` (semicolon-separated dependency items, see syntax below)

### Sheet: `Resources`
Required columns:
- `ResourceID` (string, unique)
- `Name` (string)

Optional columns:
- `Skills` (semicolon-separated skills, e.g. `ELEC;QA`)

### Sheet: `Unavailability` (optional)
Required columns (if sheet exists):
- `ResourceID`
- `StartDateTime`
- `EndDateTime`

Optional columns:
- `Reason`

Notes:
- Each row blocks a resource in `[StartDateTime, EndDateTime)`.
- Resource must exist in `Resources`.

### Sheet: `Preassigned` (optional)
Required columns (if sheet exists):
- `TaskID`
- `ResourceIDs` (semicolon-separated)
- `StartDateTime`
- `EndDateTime`

Optional columns:
- `Mode` (`HARD` or `SOFT`, default `HARD`)

Notes:
- `HARD` = enforced assignment in that window.
- `SOFT` = preferred assignment (penalized if not respected).

### Sheet: `TaskWindows` (optional)
Required columns (if sheet exists):
- `TaskID`
- `StartDateTime`
- `EndDateTime`

Optional columns:
- `Mode` (`BAN`, `MUST`, `NICE`; empty values are ignored)

Notes:
- `BAN`: task is forbidden in those slots.
- `MUST`: task can run only in MUST slots (minus BAN).
- `NICE`: soft preference windows.

### Dependencies syntax
In `Tasks.Dependencies`:
- `PRED_ID:lag_hours;PRED2:lag_hours`
- lag_hours may be negative to allow overlap.
Semantics: start(task) >= end(pred) + lag

## General constraints
- Datetimes must fall inside the chosen horizon.
- Slot size is 30 or 60 minutes; datetimes are aligned to slot boundaries.
- Empty IDs are ignored; duplicate `TaskID`/`ResourceID` raise errors.

## Generazione Excel assistita da linguaggio naturale

È disponibile una pagina Streamlit aggiuntiva: **"🤖 Genera Excel da linguaggio naturale"**.

Workflow consigliato:
1. Descrivi task/risorse/vincoli in linguaggio naturale.
2. Copia il prompt guidato verso il tuo LLM preferito.
3. Incolla il JSON prodotto nella pagina.
4. Valida e scarica l'`xlsx` pronto per il solver.

Questo approccio evita inserimento manuale riga-per-riga mantenendo un formato coerente con i fogli `Tasks`, `Resources`, `Unavailability`, `Preassigned`, `TaskWindows`.

