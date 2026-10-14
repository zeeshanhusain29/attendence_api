"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded for convenience; real environment variables win)

Layout of this file
  1. Configuration / database
  2. Time and rounding helpers (R1 - R5, R8)
  3. Request models
  4. Serialisation helpers (BSON <-> API)
  5. Pipeline / query builders (shared by the real endpoints and /admin/explain)
  6. Endpoints
"""
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the real environment

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration / database
# --------------------------------------------------------------------------- #
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes instead of naive ones.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
NON_PRESENCE = ("ABSENT", "LEAVE")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent. Every index is named so that re-running is a no-op."""
    global _indexes_ready
    # employees
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_dept_emp")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_dept_joined")
    employees.create_index([("joined_on", ASCENDING)], name="ix_joined")
    # attendance_logs
    # one record per (emp_code, date): the guarantee behind race-safe punch-in; also serves per-employee ranges
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")
    # list / analytics over a date range, sorted by date desc then emp_code
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_date_emp")
    # list filtered by status
    logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_status_date_emp")
    # punch-out: latest record of an employee with punch_in <= t
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_emp_punchin")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    # MongoDB may still be starting; retry briefly, but never block readiness for long.
    deadline = time.monotonic() + 10
    while True:
        try:
            ensure_indexes()
            break
        except PyMongoError as exc:  # pragma: no cover - depends on environment
            if time.monotonic() > deadline:
                log.error("could not create indexes at startup: %s (will retry on /health)", exc)
                break
            time.sleep(1)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database error"})


# --------------------------------------------------------------------------- #
# 2. Time and rounding helpers
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
MS_MIN, MS_MAX = 100_000_000_000, 4_102_444_800_000

EpochMs = Annotated[StrictInt, Field(ge=MS_MIN, le=MS_MAX)]


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """BSON datetime -> epoch milliseconds (exact integer arithmetic, no float)."""
    if dt is None:
        return None
    if dt.tzinfo is None:  # naive datetimes are UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _invalid(name: str, msg: str, where: str = "query") -> RequestValidationError:
    """A 422 with FastAPI's usual body shape."""
    return RequestValidationError([{"type": "value_error", "loc": (where, name), "msg": msg, "input": None}])


def _missing(name: str) -> RequestValidationError:
    return RequestValidationError([{"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None}])


def parse_date(value: str, name: str = "date") -> date_cls:
    if not DATE_RE.match(value):
        raise _invalid(name, "must be a date in YYYY-MM-DD format")
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        raise _invalid(name, "is not a valid calendar date")


def month_bounds(month: str) -> tuple[str, str, str]:
    """(first day, first day of next month, last day) as YYYY-MM-DD strings."""
    if not MONTH_RE.match(month):
        raise _invalid("month", "must be YYYY-MM")
    y, m = int(month[:4]), int(month[5:])
    if y < 1:
        raise _invalid("month", "year out of range")
    first = date_cls(y, m, 1)
    nxt = date_cls(y + 1, 1, 1) if m == 12 else date_cls(y, m + 1, 1)
    return first.isoformat(), nxt.isoformat(), (nxt - timedelta(days=1)).isoformat()


def hhmm_minutes(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm_minutes(shift_end) <= hhmm_minutes(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch-in belongs to the previous day."""
    ist = aware(punch_in).astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end):
        seconds_of_day = ist.hour * 3600 + ist.minute * 60 + ist.second
        if seconds_of_day < hhmm_minutes(shift_end) * 60:
            d -= timedelta(days=1)
    return d.isoformat()


def _ist_at(date_str: str, hhmm: str, plus_days: int = 0) -> datetime:
    d = date_cls.fromisoformat(date_str) + timedelta(days=plus_days)
    return datetime(d.year, d.month, d.day, int(hhmm[:2]), int(hhmm[3:]), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2: strictly more than 10:00 after shift start -> whole minutes since shift start, else 0."""
    delta = (aware(punch_in) - _ist_at(date_str, shift_start)).total_seconds()
    return int(delta // 60) if delta > 600 else 0


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: whole minutes between shift end and punch-out, only if >= 30."""
    end = _ist_at(date_str, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    minutes = int((aware(punch_out) - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up (Decimal, not float round())."""
    seconds = int((aware(punch_out) - aware(punch_in)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up(value: float, places: int) -> float:
    """R8. Goes through repr() so that 0.125 is treated as 0.125, not 0.12499999..."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(repr(float(value))).quantize(q, rounding=ROUND_HALF_UP))


def derive(emp: dict, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], date_str: str) -> dict:
    """All derived fields in one place (DATA_MODEL.md section 3 table)."""
    if status in NON_PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, emp["shift_start"], date_str)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    wh = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": wh,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], date_str),
        "half_day": wh < 4.5,
    }


# --------------------------------------------------------------------------- #
# 3. Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    shift_end: str = Field(default="18:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("must be YYYY-MM-DD")
        date_cls.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # An explicit null is rejected on purpose; "omitted" is the only way to leave a field unchanged.
    status: Status = Field(default=None)
    punch_in: EpochMs = Field(default=None)
    punch_out: EpochMs = Field(default=None)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 4. Serialisation helpers
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def _history_value(key: str, value: Any) -> Any:
    return to_ms(value) if key in ("punch_in", "punch_out") else value


def record_out(doc: dict) -> dict:
    history = []
    for h in doc.get("history") or []:
        history.append({
            "at": to_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {
                k: {"from": _history_value(k, v.get("from")), "to": _history_value(k, v.get("to"))}
                for k, v in (h.get("changes") or {}).items()
            },
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def num(x: Any) -> Any:
    """4.0 -> 4 so that whole numbers do not show up as floats; leaves everything else alone."""
    return int(x) if isinstance(x, float) and x.is_integer() else x


def json_safe(obj: Any) -> Any:
    """Explain output contains BSON types (dates, Int64, ...): make it plain JSON."""
    return json.loads(json_util.dumps(obj, json_options=json_util.RELAXED_JSON_OPTIONS))


# --------------------------------------------------------------------------- #
# 5. Query / pipeline builders  (the endpoints AND /admin/explain both call these)
# --------------------------------------------------------------------------- #
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]
SKIP_LIMIT = 10**12  # a skip beyond this is certainly past the end of the data (and would overflow BSON)


def build_attendance_filter(emp_code, date_from, date_to, status) -> dict:
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise _invalid("date_from", "date_from must not be after date_to")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


def _pv() -> dict:
    """Presence value of one log: 1 (or 0.5 for a half day) for PRESENT/WFH/ON_DUTY, else 0."""
    return {"$cond": [{"$in": ["$status", PRESENCE]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def _is_weekday() -> dict:
    """Mon-Fri, from the stored YYYY-MM-DD string."""
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}


def _late_flag() -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}


def build_monthly_pipeline(emp_code: str, month: str) -> list:
    start, nxt, _ = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lt": nxt}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [_is_weekday(), _pv(), 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": _late_flag()},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def working_days(month: str, joined_on: str) -> int:
    """R7: Mon-Fri in the month, counted from joined_on for a mid-month joiner."""
    first, nxt, _ = month_bounds(month)
    d = max(date_cls.fromisoformat(first), date_cls.fromisoformat(joined_on))
    end = date_cls.fromisoformat(nxt)
    n = 0
    while d < end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def build_summary_pipeline(month: str, department: Optional[str]) -> list:
    """
    Starts from employees (so people with zero logs are counted) and joins each one's month of logs.
    The inner pipeline collapses an employee's logs into one small summary document, so the outer
    $group only sees one row per employee.
    """
    start, nxt, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last}}  # R9
    if department:
        match["department"] = department
    has_wh = {"$and": [{"$in": ["$status", PRESENCE]}, {"$isNumber": "$work_hours"}]}
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lt": nxt}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [_is_weekday(), _pv(), 0]}},
                    # work hours are summed in hundredths (integers) so the average can be rounded exactly later
                    "wh_hundredths": {"$sum": {"$cond": [has_wh, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
                    "wh_count": {"$sum": {"$cond": [has_wh, 1, 0]}},
                    "late_count": {"$sum": _late_flag()},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "s",
        }},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$s.present_days"},
            "wh_hundredths": {"$sum": "$s.wh_hundredths"},
            "wh_count": {"$sum": "$s.wh_count"},
            "late_count": {"$sum": "$s.late_count"},
            "total_late_minutes": {"$sum": "$s.total_late_minutes"},
            "leave_count": {"$sum": "$s.leave_count"},
            "on_duty_count": {"$sum": "$s.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def build_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, nxt, _ = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lt": nxt}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped here
    ]
    if department:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank and the next rank is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return pipeline


def build_trend_pipeline(department: str, date_from: str, date_to: str, emp_codes: list) -> list:
    d0, d1 = date_cls.fromisoformat(date_from), date_cls.fromisoformat(date_to)
    lo = datetime(d0.year, d0.month, d0.day, tzinfo=UTC)
    hi = datetime(d1.year, d1.month, d1.day, tzinfo=UTC) + timedelta(days=1)  # densify's upper bound is exclusive
    return [
        {"$match": {"date": {"$gte": date_from, "$lte": date_to}, "emp_code": {"$in": emp_codes}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": _pv()}, "late_count": {"$sum": _late_flag()}}},
        # guarantee that $densify always has at least one document to work from, even if the range has no logs
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}},
            {"$limit": 1},
            {"$project": {"_id": {"$literal": date_from}, "present_count": {"$literal": 0}, "late_count": {"$literal": 0}}},
        ]}},
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # one document for every calendar day in [from, to]
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$set": {
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        {"$lookup": {  # R9: headcount per day
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"department": department}},
                {"$match": {"$expr": {"$lte": ["$joined_on", "$$d"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$set": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        # rates are carried as integers in 1/10000 units: exact half-up rounding and exact averaging
        {"$set": {"rate_int": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$add": [{"$divide": [{"$multiply": ["$present_count", 10000]}, "$headcount"]}, 0.5]}},
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            # current row + 6 before it, only rows inside the requested range exist; $avg ignores nulls
            "output": {"avg_int": {"$avg": "$rate_int", "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0,
            "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [{"$lte": ["$rate_int", None]}, None, {"$divide": ["$rate_int", 10000]}]},
            "moving_avg_7d": {"$cond": [
                {"$lte": ["$avg_int", None]}, None,
                {"$divide": [{"$floor": {"$add": ["$avg_int", 0.5]}}, 10000]},
            ]},
        }},
    ]


def explain_command(inner: dict) -> dict:
    return {"explain": inner, "verbosity": "executionStats"}


# --------------------------------------------------------------------------- #
# 6. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# ---- employees ------------------------------------------------------------ #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = utc_now()
    try:
        employees.insert_one(doc)  # the unique index on emp_code is the only duplicate check
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = employees.count_documents(q) if q else employees.estimated_document_count()
    skip = (page - 1) * page_size
    items = []
    if skip < SKIP_LIMIT:
        items = [employee_out(d) for d in employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---- attendance ----------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else utc_now()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, body.status, ts, None, d),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else utc_now()
    rec = logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = aware(rec["punch_in"])
    if ts <= p_in:
        raise _invalid("punched_at", "must be after punch_in", "body")
    if ts - p_in > timedelta(hours=24):
        raise _invalid("punched_at", "must be within 24 hours of punch_in", "body")
    new_vals = derive(emp, rec["status"], p_in, ts, rec["date"])
    # Atomic compare-and-set: only succeeds while punch_out is still empty, so one of two racing requests loses.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **new_vals}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_filter(emp_code, date_from, date_to, status)
    total = logs.count_documents(q) if q else logs.estimated_document_count()
    skip = (page - 1) * page_size
    items = []
    if skip < SKIP_LIMIT:
        items = [record_out(d) for d in logs.find(q).sort(ATTENDANCE_SORT).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date")
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    cur_status = rec["status"]
    cur_in, cur_out = aware(rec.get("punch_in")), aware(rec.get("punch_out"))
    new_status = body.status or cur_status

    if new_status in NON_PRESENCE:
        if body.punch_in is not None or body.punch_out is not None:
            raise _invalid("punch_in", f"punch times cannot be supplied with status {new_status}", "body")
        new_in = new_out = None
    else:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else cur_in
        new_out = from_ms(body.punch_out) if body.punch_out is not None else cur_out
        if new_in is None:
            raise _invalid("punch_in", f"status {new_status} requires a punch_in", "body")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise _invalid("punch_in", "must stay on the record's attendance date", "body")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise _invalid("punch_out", "must be after punch_in and within 24 hours of it", "body")

    if (new_status, new_in, new_out) == (cur_status, cur_in, cur_out):
        raise _invalid("body", "the request changes nothing", "body")

    new_state = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                 **derive(emp, new_status, new_in, new_out, date)}
    old_state = {
        "status": cur_status, "punch_in": cur_in, "punch_out": cur_out,
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old_state[k], "to": v} for k, v in new_state.items() if old_state[k] != v}
    now = datetime.now(UTC)
    entry = {
        "at": now.replace(microsecond=now.microsecond // 1000 * 1000),  # BSON dates hold milliseconds
        "by": body.regularized_by, "reason": body.reason, "changes": changes,
    }

    # Optimistic concurrency: the write only applies if the record is exactly as we read it
    # (same status/times and same number of history entries). The loser gets 409 instead of losing an entry.
    guard: dict = {"_id": rec["_id"], "status": cur_status,
                   "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")}
    history = rec.get("history")
    if isinstance(history, list):
        guard["history"] = {"$size": len(history)}
        update = {"$set": new_state, "$push": {"history": entry}}
    else:  # legacy record without a history field
        guard["history"] = None
        update = {"$set": {**new_state, "history": [entry]}}
    updated = logs.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, please retry")
    return record_out(updated)


# ---- analytics ------------------------------------------------------------ #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    month_bounds(month)
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    agg = next(iter(logs.aggregate(build_monthly_pipeline(emp_code, month))), None) or {}
    wd = working_days(month, emp["joined_on"])
    present = agg.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": num(present),
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(present / wd * 100, 2) if wd else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    items = []
    for r in employees.aggregate(build_summary_pipeline(month, department)):
        cnt = int(r["wh_count"])
        avg = None
        if cnt:  # exact rational -> 2 decimals, half-up
            avg = float((Decimal(int(r["wh_hundredths"])) / Decimal(100 * cnt)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": num(round_half_up(r["present_days"], 2)),
            "avg_work_hours": avg,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    return {"month": month, "items": list(logs.aggregate(build_leaderboard_pipeline(month, limit, department)))}


def _trend_codes(department: str, date_from: str, date_to: str) -> list:
    """Validates the range and returns the department's emp_codes (404 if the department has no employees)."""
    d0, d1 = parse_date(date_from, "from"), parse_date(date_to, "to")
    if d1 < d0:
        raise _invalid("to", "to must not be before from")
    if (d1 - d0).days + 1 > 92:
        raise _invalid("to", "range must not exceed 92 days")
    codes = employees.distinct("emp_code", {"department": department})
    if not codes:
        raise HTTPException(404, "department not found")
    return codes


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    codes = _trend_codes(department, date_from, date_to)
    rows = list(logs.aggregate(build_trend_pipeline(department, date_from, date_to, codes)))
    for r in rows:
        r["present_count"] = num(r["present_count"])
    return {"department": department, "items": rows}


# ---- explain -------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def need(**params):
        for k, v in params.items():
            if not v:
                raise _missing(k)

    coll = "attendance_logs"
    if endpoint == "attendance_list":
        q = build_attendance_filter(emp_code, date_from, date_to, status)
        inner = {"find": coll, "filter": q, "sort": dict(ATTENDANCE_SORT),
                 "skip": min((page - 1) * page_size, SKIP_LIMIT), "limit": page_size}
    elif endpoint == "employee_monthly":
        need(emp_code=emp_code, month=month)
        inner = {"aggregate": coll, "pipeline": build_monthly_pipeline(emp_code, month), "cursor": {}}
    elif endpoint == "department_summary":
        need(month=month)
        coll = "employees"
        inner = {"aggregate": coll, "pipeline": build_summary_pipeline(month, department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        need(month=month)
        inner = {"aggregate": coll, "pipeline": build_leaderboard_pipeline(month, limit, department), "cursor": {}}
    else:  # department_trend
        need(department=department, **{"from": from_, "to": to})
        codes = _trend_codes(department, from_, to)
        inner = {"aggregate": coll, "pipeline": build_trend_pipeline(department, from_, to, codes), "cursor": {}}

    return {"endpoint": endpoint, "collection": coll, "explain": json_safe(db.command(explain_command(inner)))}
