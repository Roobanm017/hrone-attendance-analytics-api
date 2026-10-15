"""
Employee Attendance & Analytics API.
Run from the project root: python -m uvicorn app.main:app --port 8000
"""
import os
import re
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path as ApiPath
from pydantic import BaseModel, Field, field_validator, ConfigDict
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENT | {"ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_punchin")
    logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="ix_attendance_status_date")


def half_up(value, places=2):
    if value is None:
        return None
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def to_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def epoch_ms(dt):
    dt = to_utc(dt)
    return int(dt.timestamp() * 1000) if dt else None


def from_epoch_ms(value):
    if value is None:
        return datetime.now(UTC).replace(microsecond=0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 100000000000 or value > 4102444800000:
        raise ValueError("must be a valid epoch timestamp in milliseconds")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise HTTPException(422, "date must be YYYY-MM-DD")


def parse_month(value):
    if not re.fullmatch(r"\d{4}-\d{2}", value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    try:
        y, m = map(int, value.split("-"))
        return date(y, m, 1)
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM")


def parse_hhmm(value):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value or ""):
        raise ValueError("time must be HH:MM")
    h, m = map(int, value.split(":"))
    return time(h, m)


def as_ist(dt):
    return to_utc(dt).astimezone(IST)


def shift_start_for_day(day, hhmm):
    t = parse_hhmm(hhmm)
    return datetime.combine(day, t, IST)


def attendance_day(punch_in, emp):
    local = as_ist(punch_in)
    day = local.date()
    start = parse_hhmm(emp["shift_start"])
    end = parse_hhmm(emp["shift_end"])
    if end <= start and local.time().replace(tzinfo=None) < end:
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late(punch_in, emp, att_date):
    if punch_in is None:
        return 0
    day = date.fromisoformat(att_date)
    start = shift_start_for_day(day, emp["shift_start"]).astimezone(UTC)
    seconds = (to_utc(punch_in) - start).total_seconds()
    mins = int(seconds // 60)
    return mins if seconds > 600 else 0


def shift_end_datetime(att_date, emp):
    day = date.fromisoformat(att_date)
    start = parse_hhmm(emp["shift_start"])
    end = parse_hhmm(emp["shift_end"])
    end_day = day + timedelta(days=1) if end <= start else day
    return shift_start_for_day(end_day, emp["shift_end"]).astimezone(UTC)


def derived_values(punch_in, punch_out, emp, att_date, status):
    if status not in PRESENT:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late(punch_in, emp, att_date)
    if punch_out is None or punch_in is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    seconds = (to_utc(punch_out) - to_utc(punch_in)).total_seconds()
    hours = half_up(seconds / 3600, 2)
    overtime_secs = (to_utc(punch_out) - shift_end_datetime(att_date, emp)).total_seconds()
    overtime = int(overtime_secs // 60) if overtime_secs >= 1800 else 0
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.5}


def serialize_doc(doc):
    if not doc:
        return None
    result = {k: v for k, v in doc.items() if k != "_id"}
    for key in ("created_at", "punch_in", "punch_out"):
        if key in result:
            result[key] = epoch_ms(result[key])
    history = []
    for entry in result.get("history", []) or []:
        item = dict(entry)
        item["at"] = epoch_ms(item.get("at"))
        changes = {}
        for field, change in (item.get("changes") or {}).items():
            change = dict(change)
            if field in ("punch_in", "punch_out"):
                change["from"] = epoch_ms(change.get("from"))
                change["to"] = epoch_ms(change.get("to"))
            changes[field] = change
        item["changes"] = changes
        history.append(item)
    result["history"] = history
    if "half_day" not in result:
        result["half_day"] = False
    if "late_minutes" not in result:
        result["late_minutes"] = 0
    if "overtime_minutes" not in result:
        result["overtime_minutes"] = 0
    return result


def page_params(page, page_size):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "page must be >= 1 and page_size must be between 1 and 100")
    return (page - 1) * page_size


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift(cls, v):
        parse_hhmm(v)
        return v

    @field_validator("joined_on")
    @classmethod
    def valid_joined(cls, v):
        date.fromisoformat(v)
        return v

    @field_validator("shift_end")
    @classmethod
    def shift_not_equal(cls, v, info):
        start = info.data.get("shift_start")
        if start and start == v:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_ms(cls, v):
        if v is not None:
            from_epoch_ms(v)
        return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_ms(cls, v):
        if v is not None:
            from_epoch_ms(v)
        return v


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_ms(cls, v):
        if v is not None:
            from_epoch_ms(v)
        return v


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return {**{k: v for k, v in doc.items() if k != "_id"}, "created_at": epoch_ms(doc["created_at"])}


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    skip = page_params(page, page_size)
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    items = list(employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    for item in items:
        item["created_at"] = epoch_ms(item.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    try:
        ts = from_epoch_ms(body.punched_at)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    att_date = attendance_day(ts, emp)
    vals = derived_values(ts, None, emp, att_date, body.status)
    doc = {"emp_code": body.emp_code, "date": att_date, "status": body.status, "punch_in": ts,
           "punch_out": None, "work_hours": None, **vals, "history": []}
    try:
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    try:
        ts = from_epoch_ms(body.punched_at)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    record = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts, "$ne": None}},
                           sort=[("punch_in", DESCENDING)])
    if not record:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    elapsed = (ts - to_utc(record["punch_in"])).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(422, "punched_at must be after punch_in and no more than 24 hours later")
    vals = derived_values(record["punch_in"], ts, emp, record["date"], record["status"])
    updated = logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **vals}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record already punched out")
    return serialize_doc(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    skip = page_params(page, page_size)
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            parse_date(date_from)
            q["date"]["$gte"] = date_from
        if date_to:
            parse_date(date_to)
            q["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
    if status:
        if status not in STATUSES:
            raise HTTPException(422, "invalid status")
        q["status"] = status
    total = logs.count_documents(q)
    docs = list(logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size))
    return {"items": [serialize_doc(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date_str}")
def regularize(emp_code: str, date_str: str, body: RegularizeIn):
    parse_date(date_str)
    key = {"emp_code": emp_code, "date": date_str}
    rec = logs.find_one(key)
    if not rec:
        raise HTTPException(404, "attendance record not found")
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    old = {k: rec.get(k) for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    new_status = body.status if body.status is not None else rec.get("status", "PRESENT")
    pi = from_epoch_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
    po = from_epoch_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
    if new_status in PRESENT and pi is None:
        raise HTTPException(422, "presence status requires punch_in")
    if new_status in {"ABSENT", "LEAVE"}:
        pi, po = None, None
    if pi and po and (to_utc(po) <= to_utc(pi) or (to_utc(po)-to_utc(pi)).total_seconds() > 86400):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    vals = derived_values(pi, po, emp, date_str, new_status)
    new = {"status": new_status, "punch_in": pi, "punch_out": po, **vals}
    changes = {}
    for field, value in new.items():
        previous = old.get(field)
        if previous != value:
            changes[field] = {"from": previous, "to": value}
    if not changes:
        raise HTTPException(422, "correction does not change any fields")
    entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    logs.update_one(key, {"$set": new, "$push": {"history": entry}})
    return serialize_doc(logs.find_one(key))


def month_bounds(month):
    first = parse_month(month)
    last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
    return first.isoformat(), last.isoformat(), first, last


def working_days(start, end):
    if start > end:
        return 0
    total = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            total += 1
        d += timedelta(days=1)
    return total


def rounded_rate(numerator, denominator):
    return half_up(numerator / denominator, 4) if denominator else None


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    first_s, last_s, first, last = month_bounds(month)
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    start = max(first, date.fromisoformat(emp["joined_on"]))
    workdays = working_days(start, last)
    rows = list(logs.find({"emp_code": emp_code, "date": {"$gte": first_s, "$lte": last_s}}))
    present_days = sum((0.5 if r.get("half_day", False) else 1.0) for r in rows if r.get("status") in PRESENT and date.fromisoformat(r["date"]).weekday() < 5)
    leave_days = sum(1 for r in rows if r.get("status") == "LEAVE")
    late_rows = [r for r in rows if (r.get("late_minutes") or 0) > 0]
    return {"emp_code": emp_code, "month": month, "working_days": workdays, "present_days": half_up(present_days, 2),
            "leave_days": leave_days, "late_count": len(late_rows),
            "total_late_minutes": sum(r.get("late_minutes", 0) or 0 for r in rows),
            "total_overtime_minutes": sum(r.get("overtime_minutes", 0) or 0 for r in rows),
            "attendance_pct": rounded_rate(present_days, workdays)}


@app.get("/analytics/departments/summary")
def department_summary(month: str):
    first_s, last_s, first, last = month_bounds(month)
    active_emps = list(employees.find({"joined_on": {"$lte": last.isoformat()}}, {"_id": 0, "emp_code": 1, "department": 1}))
    grouped = {}
    for emp in active_emps:
        grouped.setdefault(emp["department"], {"headcount": 0, "codes": set()})
        grouped[emp["department"]]["headcount"] += 1
        grouped[emp["department"]]["codes"].add(emp["emp_code"])
    rows = list(logs.find({"date": {"$gte": first_s, "$lte": last_s}}))
    out = []
    for department, info in sorted(grouped.items()):
        drows = [r for r in rows if r.get("emp_code") in info["codes"]]
        hours = [r["work_hours"] for r in drows if r.get("work_hours") is not None]
        present = sum((0.5 if r.get("half_day", False) else 1.0) for r in drows if r.get("status") in PRESENT and date.fromisoformat(r["date"]).weekday() < 5)
        out.append({"department": department, "headcount": info["headcount"], "present_days": half_up(present, 2),
                    "avg_work_hours": half_up(sum(hours)/len(hours), 2) if hours else None,
                    "late_count": sum(1 for r in drows if (r.get("late_minutes") or 0) > 0),
                    "total_late_minutes": sum(r.get("late_minutes", 0) or 0 for r in drows),
                    "leave_count": sum(1 for r in drows if r.get("status") == "LEAVE"),
                    "on_duty_count": sum(1 for r in drows if r.get("status") == "ON_DUTY")})
    return {"month": month, "items": out}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=100)):
    first_s, last_s, _, _ = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first_s, "$lte": last_s}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    try:
        items = list(logs.aggregate(pipeline))
    except PyMongoError:
        # Compatibility fallback for MongoDB versions without $setWindowFields.
        grouped = list(logs.aggregate(pipeline[:4] + [{"$project": {"_id": 1, "total_late_minutes": 1, "late_count": 1, "emp": 1}}]))
        grouped.sort(key=lambda x: (-x["total_late_minutes"], x["_id"]))
        items = []
        prev, rank = None, 0
        for idx, row in enumerate(grouped, 1):
            if row["total_late_minutes"] <= 0: continue
            if row["total_late_minutes"] != prev: rank = idx
            prev = row["total_late_minutes"]
            if rank > limit: continue
            e = row["emp"][0]
            items.append({"rank": rank, "emp_code": row["_id"], "name": e["name"], "department": e["department"],
                          "total_late_minutes": row["total_late_minutes"], "late_count": row["late_count"]})
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str, date_to: str):
    start, end = parse_date(date_from), parse_date(date_to)
    if start > end:
        raise HTTPException(422, "date_from must be <= date_to")
    if (end - start).days > 366:
        raise HTTPException(422, "date range must not exceed 367 calendar days")
    # Aggregate once, then fill dates for the requested period in memory.
    eligible = list(employees.find({"department": department, "joined_on": {"$lte": end.isoformat()}}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    codes = {e["emp_code"] for e in eligible}
    records = list(logs.find({"emp_code": {"$in": list(codes)}, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}})) if codes else []
    by_day = {}
    for r in records:
        by_day.setdefault(r["date"], []).append(r)
    items, rates = [], []
    day = start
    while day <= end:
        headcount = sum(1 for e in eligible if e["joined_on"] <= day.isoformat())
        dayrows = by_day.get(day.isoformat(), [])
        present = sum((0.5 if r.get("half_day", False) else 1.0) for r in dayrows if r.get("status") in PRESENT)
        late = sum(1 for r in dayrows if (r.get("late_minutes") or 0) > 0)
        rate = rounded_rate(present, headcount)
        rates.append(rate)
        window = [v for v in rates[-7:] if v is not None]
        moving = half_up(sum(window)/len(window), 4) if window else None
        items.append({"date": day.isoformat(), "is_working_day": day.weekday() < 5, "headcount": headcount,
                      "present_count": half_up(present, 2), "late_count": late, "attendance_rate": rate, "moving_avg_7d": moving})
        day += timedelta(days=1)
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain(endpoint: str):
    # Allowlisted query plans. executionStats are real explain results from MongoDB.
    plans = {
        "employees": (employees, [{"$match": {}}, {"$sort": {"emp_code": 1}}, {"$limit": 20}]),
        "attendance": (logs, [{"$match": {}}, {"$sort": {"date": -1, "emp_code": 1}}, {"$limit": 20}]),
        "attendance/punch-in": (logs, [{"$match": {"emp_code": "EMP0001", "date": "2026-01-01"}}]),
        "analytics/leaderboard/late": (logs, [{"$match": {"date": {"$gte": "2026-01-01", "$lte": "2026-01-31"}}},
                                               {"$group": {"_id": "$emp_code", "late": {"$sum": "$late_minutes"}}}]),
    }
    if endpoint not in plans:
        raise HTTPException(404, "unknown endpoint name")
    collection, pipeline = plans[endpoint]
    try:
        result = db.command("explain", {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}},
                            verbosity="executionStats")
        return result
    except PyMongoError as exc:
        raise HTTPException(503, f"explain failed: {exc.__class__.__name__}")


@app.get("/")
def root():
    return {"service": "Employee Attendance & Analytics API", "docs": "/docs"}
