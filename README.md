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

### Dependencies syntax
In `Tasks.Dependencies`:
- `PRED_ID:lag_hours;PRED2:lag_hours`
- lag_hours may be negative to allow overlap.
Semantics: start(task) >= end(pred) + lag
