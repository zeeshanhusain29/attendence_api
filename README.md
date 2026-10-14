# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # or an Atlas URI; a local .env also works
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotent). MongoDB 6.0+ is required (`$densify`, `$setWindowFields`).

## Status
All endpoints in `openapi.yaml` are implemented, including `/admin/explain/{endpoint}`.

## Notes
<!-- write here anything that is missing or that the reviewers should know -->
