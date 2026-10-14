"""
Edge-case checker for the analytics endpoints. NOT part of the submission.

WARNING: wipes the `employees` and `attendance_logs` collections of MONGO_DB. Use a throwaway database.

  1.  export MONGO_URI="mongodb://localhost:27017"; export MONGO_DB=attendance_check
  2.  uvicorn app.main:app --port 8000          (same env vars, separate terminal)
  3.  python check_analytics.py

It seeds awkward data (ties, an employee with no logs, a mid-month joiner, someone who joins next month,
weekend records, missing weekdays, logs of an unknown employee, legacy records without half_day) and compares every
analytics endpoint with a slow, obvious, plain-Python implementation of the rules in openapi.yaml.
"""
import json
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import httpx
from pymongo import MongoClient

BASE = os.getenv("BASE_URL", "http://localhost:8000")
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[os.getenv("MONGO_DB", "attendance_check")]
MONTH = "2026-07"
PRES = {"PRESENT", "WFH", "ON_DUTY"}
rnd = random.Random(7)


def hu(x, places):  # half-up
    return float(Decimal(repr(float(x))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# ----------------------------------------------------------------------------- seed
EMPS = [
    ("EMP0001", "Eng", "2026-01-05", "09:30", "18:30"),
    ("EMP0002", "Eng", "2026-07-15", "09:30", "18:30"),   # mid-month joiner
    ("EMP0003", "Eng", "2026-02-01", "09:30", "18:30"),   # no logs at all
    ("EMP0004", "Eng", "2026-08-01", "09:30", "18:30"),   # joins next month
    ("EMP0005", "Ops", "2026-03-01", "22:00", "06:00"),   # overnight
    ("EMP0006", "Ops", "2026-03-01", "09:30", "18:30"),
    ("EMP0007", "Ops", "2026-03-01", "09:30", "18:30"),
    ("EMP0008", "HR", "2026-07-20", "09:30", "18:30"),    # department with a single late joiner
]
employees = [dict(emp_code=c, name=f"Name {c}", email=f"{c}@x.com", department=d, joined_on=j, shift_start=s, shift_end=e,
                  created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)) for c, d, j, s, e in EMPS]
logs = []
LATES = [0, 0, 0, 15, 15, 30, 45, 45, 60]  # repeated values -> ties
for code, dept, joined, *_ in EMPS:
    if code in ("EMP0003", "EMP0004"):
        continue
    d = date(2026, 7, 1)
    while d <= date(2026, 7, 31):
        if d.isoformat() >= joined and rnd.random() < (0.35 if d.weekday() >= 5 else 0.85):  # some weekdays are skipped
            status = rnd.choices(["PRESENT", "WFH", "ON_DUTY", "LEAVE", "ABSENT"], [10, 3, 2, 1, 1])[0]
            if status in PRES:
                wh = round(rnd.choice([3.5, 4.49, 4.5, 4.51, 8.0, 8.12, 8.125, 9.0, 9.5]), 2)
                late = rnd.choice(LATES)
                rec = dict(punch_in=datetime(d.year, d.month, d.day, 4, 0, tzinfo=timezone.utc),
                           punch_out=datetime(d.year, d.month, d.day, 13, 0, tzinfo=timezone.utc),
                           work_hours=wh, late_minutes=late, overtime_minutes=rnd.choice([0, 0, 30, 45]), half_day=wh < 4.5)
            else:
                rec = dict(punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
            rec.update(emp_code=code, date=d.isoformat(), status=status, history=[])
            if rnd.random() < 0.1:  # legacy record: no history, no half_day
                rec.pop("history"); rec.pop("half_day")
            logs.append(rec)
        d += timedelta(days=1)
logs.append(dict(emp_code="EMP9999", date="2026-07-06", status="PRESENT", punch_in=None, punch_out=None, work_hours=8.0,
                 late_minutes=500, overtime_minutes=0, half_day=False, history=[]))  # unknown employee: must be ignored
db.employees.delete_many({}); db.attendance_logs.delete_many({})
db.employees.insert_many(employees); db.attendance_logs.insert_many(logs)
for r in logs:
    r.pop("_id", None)
EMP = {e["emp_code"]: e for e in employees}
LOGS = [r for r in logs if r["emp_code"] in EMP]


# ----------------------------------------------------------------------------- reference implementation (slow & obvious)
def weekday(ds): return date.fromisoformat(ds).weekday() < 5
def pv(r): return (0.5 if r.get("half_day") else 1) if r["status"] in PRES else 0
def in_month(r, m=MONTH): return r["date"].startswith(m)


def ref_monthly(code):
    e = EMP[code]; rs = [r for r in LOGS if r["emp_code"] == code and in_month(r)]
    d, wd = date(2026, 7, 1), 0
    while d.month == 7:
        wd += (d.weekday() < 5 and d.isoformat() >= e["joined_on"]); d += timedelta(days=1)
    present = sum(pv(r) for r in rs if weekday(r["date"]))
    return dict(emp_code=code, month=MONTH, working_days=wd, present_days=present,
                leave_days=sum(r["status"] == "LEAVE" for r in rs), late_count=sum(r["late_minutes"] > 0 for r in rs),
                total_late_minutes=sum(r["late_minutes"] for r in rs), total_overtime_minutes=sum(r["overtime_minutes"] for r in rs),
                attendance_pct=hu(present / wd * 100, 2) if wd else None)


def ref_summary():
    out = {}
    for e in EMP.values():
        if e["joined_on"] > "2026-07-31":
            continue
        o = out.setdefault(e["department"], dict(department=e["department"], headcount=0, present_days=0, _wh=[], late_count=0,
                                                 total_late_minutes=0, leave_count=0, on_duty_count=0))
        o["headcount"] += 1
        for r in LOGS:
            if r["emp_code"] == e["emp_code"] and in_month(r):
                o["present_days"] += pv(r) if weekday(r["date"]) else 0
                if r["status"] in PRES and r.get("work_hours") is not None:
                    o["_wh"].append(Decimal(str(r["work_hours"])))
                o["late_count"] += r["late_minutes"] > 0; o["total_late_minutes"] += r["late_minutes"]
                o["leave_count"] += r["status"] == "LEAVE"; o["on_duty_count"] += r["status"] == "ON_DUTY"
    res = []
    for k in sorted(out):
        o = out[k]; wh = o.pop("_wh")
        o["avg_work_hours"] = float((sum(wh) / len(wh)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if wh else None
        res.append(o)
    return res


def ref_leaderboard(limit, dept=None):
    tot = {}
    for r in LOGS:
        if in_month(r) and r["late_minutes"] > 0 and (dept is None or EMP[r["emp_code"]]["department"] == dept):
            t = tot.setdefault(r["emp_code"], [0, 0]); t[0] += r["late_minutes"]; t[1] += 1
    rows = sorted(tot.items(), key=lambda kv: (-kv[1][0], kv[0]))
    items = []
    for i, (code, (m, c)) in enumerate(rows):
        rank = 1 + sum(1 for _, (m2, _) in rows if m2 > m)
        if rank <= limit:
            items.append(dict(rank=rank, emp_code=code, name=EMP[code]["name"], department=EMP[code]["department"], total_late_minutes=m, late_count=c))
    return items


def ref_trend(dept, a, b):
    d, end, rows, rates = date.fromisoformat(a), date.fromisoformat(b), [], []
    while d <= end:
        ds = d.isoformat(); wd = d.weekday() < 5
        hc = sum(1 for e in EMP.values() if e["department"] == dept and e["joined_on"] <= ds)
        rs = [r for r in LOGS if r["date"] == ds and EMP[r["emp_code"]]["department"] == dept]
        present = sum(pv(r) for r in rs)
        rate = hu(present / hc, 4) if wd and hc > 0 else None
        rates.append(rate)
        win = [x for x in rates[-7:] if x is not None]
        rows.append(dict(date=ds, is_working_day=wd, headcount=hc, present_count=present, late_count=sum(r["late_minutes"] > 0 for r in rs),
                         attendance_rate=rate, moving_avg_7d=hu(sum(win) / len(win), 4) if win else None))
        d += timedelta(days=1)
    return rows


# ----------------------------------------------------------------------------- compare
fails = 0


def same(x, y):
    if isinstance(x, dict) and isinstance(y, dict):
        return x.keys() == y.keys() and all(same(x[k], y[k]) for k in x)
    if isinstance(x, list) and isinstance(y, list):
        return len(x) == len(y) and all(same(a, b) for a, b in zip(x, y))
    if isinstance(x, (int, float)) and isinstance(y, (int, float)) and not isinstance(x, bool):
        return abs(x - y) < 1e-9
    return x == y


def check(name, got, want):
    global fails
    if same(got, want):
        print("ok   ", name)
    else:
        fails += 1
        print("FAIL ", name); print("   got :", json.dumps(got)[:900]); print("   want:", json.dumps(want)[:900])


c = httpx.Client(base_url=BASE, timeout=60)
assert c.get("/health").status_code == 200, "app is not running / cannot reach MongoDB"
for code in EMP:
    check(f"monthly {code}", c.get(f"/analytics/employees/{code}/monthly", params={"month": MONTH}).json(), ref_monthly(code))
check("department summary", c.get("/analytics/departments/summary", params={"month": MONTH}).json(), dict(month=MONTH, items=ref_summary()))
check("department summary (Ops only)", c.get("/analytics/departments/summary", params={"month": MONTH, "department": "Ops"}).json()["items"],
      [x for x in ref_summary() if x["department"] == "Ops"])
check("summary of an empty month is empty", c.get("/analytics/departments/summary", params={"month": "2025-01"}).json()["items"], [])
for limit in (1, 2, 3, 5, 50):
    check(f"leaderboard limit={limit}", c.get("/analytics/leaderboard/late", params={"month": MONTH, "limit": limit}).json(), dict(month=MONTH, items=ref_leaderboard(limit)))
check("leaderboard Ops limit=2", c.get("/analytics/leaderboard/late", params={"month": MONTH, "limit": 2, "department": "Ops"}).json()["items"], ref_leaderboard(2, "Ops"))
ranks = [x["rank"] for x in c.get("/analytics/leaderboard/late", params={"month": MONTH, "limit": 50}).json()["items"]]
print("      leaderboard ranks (look for gaps like 1,2,2,4):", ranks[:12])
for dept in ("Eng", "Ops", "HR"):
    for a, b in (("2026-06-28", "2026-08-05"), ("2026-07-06", "2026-07-06"), ("2026-12-01", "2026-12-07")):
        got = c.get(f"/analytics/departments/{dept}/trend", params={"from": a, "to": b}).json()
        check(f"trend {dept} {a}..{b}", got, dict(department=dept, items=ref_trend(dept, a, b)))
for ep, q in (("attendance_list", {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31", "status": "PRESENT"}),
              ("attendance_list", {}), ("employee_monthly", {"emp_code": "EMP0001", "month": MONTH}),
              ("department_summary", {"month": MONTH}), ("department_summary", {"month": MONTH, "department": "Eng"}),
              ("late_leaderboard", {"month": MONTH, "limit": 3}), ("department_trend", {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"})):
    body = c.get(f"/admin/explain/{ep}", params=q)
    txt = body.text
    ok = body.status_code == 200 and "IXSCAN" in txt and "COLLSCAN" not in txt
    fails += not ok
    print("ok   " if ok else "FAIL ", f"explain {ep} {q}", "" if ok else f"(status {body.status_code}, IXSCAN={'IXSCAN' in txt}, COLLSCAN={'COLLSCAN' in txt})")
print("\nALL CHECKS PASSED" if not fails else f"\n{fails} CHECK(S) FAILED")
sys.exit(1 if fails else 0)
