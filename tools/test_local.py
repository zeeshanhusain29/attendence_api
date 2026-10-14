import sys, threading
sys.path.insert(0, "/home/claude/candidate_kit")
import mongomock
from datetime import datetime, timezone, timedelta
import app.main as m

mc = mongomock.MongoClient(tz_aware=True)
m.client = mc; m.db = mc["t"]; m.employees = m.db["employees"]; m.logs = m.db["attendance_logs"]
from fastapi.testclient import TestClient

IST = m.IST
def ms(y, mo, d, h, mi, s=0, ms_=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000 + ms_

# ---- pure rules
def dt(h, mi, s=0, day=6): return datetime(2026, 7, day, h, mi, s, tzinfo=IST)
assert m.compute_late_minutes(dt(9,40,0), "09:30", "2026-07-06") == 0
assert m.compute_late_minutes(dt(9,40,1), "09:30", "2026-07-06") == 10
assert m.compute_late_minutes(dt(10,15,59), "09:30", "2026-07-06") == 45
assert m.compute_late_minutes(dt(9,0), "09:30", "2026-07-06") == 0
assert m.compute_work_hours(dt(9,0), dt(9,0)+timedelta(seconds=16200+18)) == 4.51  # 4.505 -> half-up
assert m.compute_work_hours(dt(9,0), dt(9,0)+timedelta(seconds=int(4.125*3600))) == 4.13  # exact tie -> up (round() gives 4.12)
assert m.compute_overtime(dt(19,0), "09:30", "18:30", "2026-07-06") == 30
assert m.compute_overtime(dt(18,59,59), "09:30", "18:30", "2026-07-06") == 0
assert m.compute_overtime(dt(6,40, day=7), "22:00", "06:00", "2026-07-06") == 40
assert m.round_half_up(0.125, 2) == 0.13 and m.round_half_up(2.675, 2) == 2.68
# overnight date rule
u = lambda h, mi, s=0, day=6: datetime(2026,7,day,h,mi,s,tzinfo=IST).astimezone(timezone.utc)
assert m.attendance_date(u(5,59,59,7), "22:00", "06:00") == "2026-07-06"
assert m.attendance_date(u(6,0,0,7), "22:00", "06:00") == "2026-07-07"
assert m.attendance_date(u(21,55), "22:00", "06:00") == "2026-07-06"
assert m.attendance_date(u(0,10,0,7), "09:30", "18:30") == "2026-07-07"   # IST date, not UTC date
print("rules OK")

with TestClient(m.app) as c:
    assert c.get("/health").json() == {"status": "ok"}
    def emp(code, dept="Eng", s="09:30", e="18:30", j="2026-01-05"):
        return c.post("/employees", json=dict(emp_code=code, name="N "+code, email="a@b.co", department=dept, shift_start=s, shift_end=e, joined_on=j))
    r = emp("EMP0001"); assert r.status_code == 201 and isinstance(r.json()["created_at"], int) and "_id" not in r.json(), r.text
    assert emp("EMP0001").status_code == 409
    assert emp("EMP0002", s="10:00", e="10:00").status_code == 422
    assert emp("EMP2", j="2026-13-01").status_code == 422
    assert emp("EMP0009", j="20260105").status_code == 422
    emp("EMP0005", s="22:00", e="06:00")
    for i in range(3, 5): emp(f"EMP000{i}", dept="Ops")
    # employees list
    r = c.get("/employees?page=1&page_size=2").json(); assert [x["emp_code"] for x in r["items"]] == ["EMP0001","EMP0003"] and r["total"] == 4
    assert c.get("/employees?department=Ops").json()["total"] == 2
    assert c.get("/employees?page=0").status_code == 422 and c.get("/employees?page_size=101").status_code == 422

    # punch-in validation
    P = "/attendance/punch-in"
    assert c.post(P, json={"emp_code":"EMP0001","punched_at":1783312500}).status_code == 422       # seconds
    assert c.post(P, json={"emp_code":"EMP0001","punched_at":1783312500000.0}).status_code == 422  # float
    assert c.post(P, json={"emp_code":"EMP0001","punched_at":"1783312500000"}).status_code == 422
    assert c.post(P, json={"emp_code":"EMP0001","punched_at":ms(2026,7,6,9,40), "status":"ABSENT"}).status_code == 422
    assert c.post(P, json={"emp_code":"NOPE","punched_at":ms(2026,7,6,9,40)}).status_code == 404
    r = c.post(P, json={"emp_code":"EMP0001","punched_at":ms(2026,7,6,9,40,1,900)}); j = r.json()
    assert r.status_code == 201 and j["late_minutes"] == 10 and j["date"] == "2026-07-06" and j["punch_in"] == ms(2026,7,6,9,40,1) and j["history"] == [] and j["work_hours"] is None, j
    assert c.post(P, json={"emp_code":"EMP0001","punched_at":ms(2026,7,6,12,0)}).status_code == 409

    # punch-out
    O = "/attendance/punch-out"
    assert c.post(O, json={"emp_code":"EMP0001","punched_at":ms(2026,7,6,9,40,1)}).status_code == 422      # == punch_in
    assert c.post(O, json={"emp_code":"EMP0001","punched_at":ms(2026,7,7,10,0)}).status_code == 422       # > 24h
    assert c.post(O, json={"emp_code":"EMP0001","punched_at":ms(2026,7,5,10,0)}).status_code == 404       # nothing before
    r = c.post(O, json={"emp_code":"EMP0001","punched_at":ms(2026,7,6,19,10)}); j = r.json()
    assert r.status_code == 200 and j["overtime_minutes"] == 40 and j["work_hours"] == 9.5 and j["half_day"] is False, j
    assert c.post(O, json={"emp_code":"EMP0001","punched_at":ms(2026,7,6,19,20)}).status_code == 409
    # overnight
    r = c.post(P, json={"emp_code":"EMP0005","punched_at":ms(2026,7,6,21,55)}); assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 0
    r = c.post(O, json={"emp_code":"EMP0005","punched_at":ms(2026,7,7,6,40)}); j = r.json()
    assert r.status_code == 200 and j["overtime_minutes"] == 40 and j["work_hours"] == 8.75, j
    # half day
    c.post(P, json={"emp_code":"EMP0003","punched_at":ms(2026,7,7,9,30), "status":"WFH"})
    j = c.post(O, json={"emp_code":"EMP0003","punched_at":ms(2026,7,7,13,0)}).json(); assert j["half_day"] is True and j["work_hours"] == 3.5

    # concurrency: 12 parallel punch-ins / punch-outs
    emp("EMP0010")
    res = []
    def go(url, body): res.append(c.post(url, json=body).status_code)
    ts = [threading.Thread(target=go, args=(P, {"emp_code":"EMP0010","punched_at":ms(2026,7,8,9,30)})) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(res) == [201] + [409]*11, res
    res.clear()
    ts = [threading.Thread(target=go, args=(O, {"emp_code":"EMP0010","punched_at":ms(2026,7,8,18,30)})) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(res) == [200] + [409]*11, res
    print("punch + races OK")

    # list attendance
    r = c.get("/attendance?emp_code=EMP0001").json(); assert r["total"] == 1 and "id" not in r["items"][0]
    assert c.get("/attendance?date_from=2026-07-09&date_to=2026-07-01").status_code == 422
    assert c.get("/attendance?date_from=2026-02-30").status_code == 422
    assert c.get("/attendance?status=NOPE").status_code == 422
    assert c.get("/attendance?page=1&page_size=100").status_code == 200
    r = c.get("/attendance?page=1&page_size=100").json(); keys = [(x["date"], x["emp_code"]) for x in r["items"]]
    assert keys == sorted(keys, key=lambda k: (-int(k[0].replace("-","")), k[1])), keys
    assert c.get("/attendance?page=99999999999999999999").status_code in (200, 422)

    # PATCH
    A = "/attendance/EMP0001/2026-07-06"
    body = dict(reason="biometric glitch", regularized_by="hr")
    assert c.patch(A, json={**body, "reason": "x"}).status_code == 422
    assert c.patch(A, json=body).status_code == 422                                   # nothing changes
    assert c.patch("/attendance/EMP0001/2026-07-07", json={**body, "status":"LEAVE"}).status_code == 404
    assert c.patch("/attendance/NOPE/2026-07-06", json={**body, "status":"LEAVE"}).status_code == 404
    assert c.patch("/attendance/EMP0001/2026-02-30", json={**body, "status":"LEAVE"}).status_code == 422
    assert c.patch(A, json={**body, "status":"LEAVE", "punch_in": ms(2026,7,6,9,0)}).status_code == 422
    assert c.patch(A, json={**body, "punch_in": ms(2026,7,7,9,0)}).status_code == 422            # other date
    assert c.patch(A, json={**body, "punch_out": ms(2026,7,6,9,0)}).status_code == 422           # before punch_in
    assert c.patch(A, json={**body, "punch_in": None}).status_code == 422
    r = c.patch(A, json={**body, "punch_in": ms(2026,7,6,9,28), "work_hours": 99, "late_minutes": 99}); j = r.json()
    assert r.status_code == 200 and j["late_minutes"] == 0 and j["work_hours"] == 9.7 and len(j["history"]) == 1, j
    ch = j["history"][0]["changes"]
    assert set(ch) == {"punch_in", "late_minutes", "work_hours"} and ch["punch_in"]["from"] == ms(2026,7,6,9,40,1) and ch["late_minutes"] == {"from": 10, "to": 0}, ch
    r = c.patch(A, json={**body, "status": "LEAVE"}); j = r.json()
    assert j["punch_in"] is None and j["work_hours"] is None and j["overtime_minutes"] == 0 and len(j["history"]) == 2, j
    assert set(j["history"][1]["changes"]) == {"status","punch_in","punch_out","work_hours","overtime_minutes"}, j["history"][1]["changes"]
    assert c.patch(A, json={**body, "status": "PRESENT"}).status_code == 422       # needs punch_in
    r = c.patch(A, json={**body, "status": "WFH", "punch_in": ms(2026,7,6,9,30), "punch_out": ms(2026,7,6,12,0)})
    assert r.status_code == 200 and r.json()["half_day"] is True and len(r.json()["history"]) == 3
    # concurrent PATCHes: history never loses an entry
    res = []
    def patch(i): res.append(c.patch(A, json={**body, "punch_out": ms(2026,7,6,13,i+1)}).status_code)
    ts = [threading.Thread(target=patch, args=(i,)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    n200 = res.count(200); assert n200 >= 1 and set(res) <= {200, 409}, res
    assert len(c.get("/attendance?emp_code=EMP0001").json()["items"][0]["history"]) == 3 + n200
    print("patch OK", res)
    # legacy record without history/half_day
    m.logs.insert_one({"emp_code":"EMP0001","date":"2026-07-14","status":"PRESENT","punch_in":datetime(2026,7,14,4,2,tzinfo=timezone.utc),"punch_out":datetime(2026,7,14,13,1,tzinfo=timezone.utc),"work_hours":8.98,"late_minutes":0,"overtime_minutes":0})
    j = c.get("/attendance?emp_code=EMP0001&date_from=2026-07-14").json()["items"][0]; assert j["history"] == [] and j["half_day"] is False
    r = c.patch("/attendance/EMP0001/2026-07-14", json={**body, "status": "ON_DUTY"}); assert r.status_code == 200 and len(r.json()["history"]) == 1, r.text
    # validation errors on analytics / explain params
    assert c.get("/analytics/employees/EMP0001/monthly?month=2026-13").status_code == 422
    assert c.get("/analytics/employees/NOPE/monthly?month=2026-07").status_code == 404
    assert c.get("/analytics/leaderboard/late?month=2026-07&limit=51").status_code == 422
    assert c.get("/analytics/departments/Eng/trend?from=2026-07-10&to=2026-07-01").status_code == 422
    assert c.get("/analytics/departments/Eng/trend?from=2026-01-01&to=2026-06-01").status_code == 422
    assert c.get("/analytics/departments/Nope/trend?from=2026-07-01&to=2026-07-10").status_code == 404
    assert c.get("/analytics/departments/Eng/trend?from=2026-07-01").status_code == 422
    assert c.get("/admin/explain/employee_monthly").status_code == 422
    assert c.get("/admin/explain/bogus").status_code == 422
    print("validation OK")
    # working days (R7)
    assert m.working_days("2026-07", "2026-01-05") == 23 and m.working_days("2026-07", "2026-07-15") == 13 and m.working_days("2026-07", "2026-08-01") == 0
    # pipeline builders at least construct
    m.build_trend_pipeline("Eng", "2026-07-01", "2026-07-31", ["EMP0001"]); m.build_summary_pipeline("2026-07", None); m.build_leaderboard_pipeline("2026-07", 3, "Eng")
print("ALL LOCAL TESTS PASSED")
