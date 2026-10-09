"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------------------
# MongoDB client and database initialization
# ---------------------------------------------------------------------------
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    # Ping MongoDB to ensure connectivity
    try:
        client.admin.command('ping')
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unreachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = page * page_size
    total = db.employees.count_documents({})
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()
    d = ts.date().isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    res = db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
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
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# ---------------------------------------------------------------------------
# Punch‑out endpoint
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    # Resolve punch‑out time
    po_dt = epoch_to_dt(body.punched_at) if body.punched_at else truncate_to_sec(datetime.now(tz=IST))
    # Find the most recent open record (punch_in <= po_dt, punch_out null)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": po_dt},
            "punch_out": None,
        },
        sort=[("date", -1), ("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "no open attendance record found")
    # Validation rules
    if po_dt <= record["punch_in"]:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (po_dt - record["punch_in"]).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at cannot be more than 24h after punch_in")
    # Compute derived fields
    work_hours = compute_work_hours(record["punch_in"], po_dt)
    overtime = compute_overtime(po_dt, emp["shift_end"], record["date"])
    half_day = work_hours < 4.5
    update_doc = {
        "punch_out": po_dt,
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }
    result = db.attendance_logs.update_one({"_id": record["_id"]}, {"$set": update_doc})
    if result.modified_count != 1:
        raise HTTPException(409, "concurrent update conflict")
    record.update(update_doc)
    record["id"] = str(record.pop("_id"))
    return record

# ---------------------------------------------------------------------------
# Regularize (manual correction) endpoint
# ---------------------------------------------------------------------------
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "attendance record not found")
    changes = {}
    # Status change validation
    if body.status:
        if body.status in ["ABSENT", "LEAVE"]:
            if body.punch_in or body.punch_out:
                raise HTTPException(422, "Cannot supply punch times when status is ABSENT or LEAVE")
        else:
            # Presence status requires a punch_in (existing or supplied)
            if not (body.punch_in or record.get("punch_in")):
                raise HTTPException(422, "Presence status requires punch_in")
        changes["status"] = {"from": record.get("status"), "to": body.status}
    # Punch‑in validation
    if body.punch_in is not None:
        pi = epoch_to_dt(body.punch_in)
        # Ensure it stays on the record's attendance date (R1)
        if pi.date().isoformat() != date:
            raise HTTPException(422, "punch_in must be on the attendance date")
        changes["punch_in"] = {"from": record.get("punch_in"), "to": pi}
    else:
        pi = record.get("punch_in")
    # Punch‑out validation
    if body.punch_out is not None:
        po = epoch_to_dt(body.punch_out)
        if pi and po <= pi:
            raise HTTPException(422, "punch_out must be after punch_in")
        if pi and (po - pi).total_seconds() > 24 * 3600:
            raise HTTPException(422, "punch_out cannot be more than 24h after punch_in")
        changes["punch_out"] = {"from": record.get("punch_out"), "to": po}
    else:
        po = record.get("punch_out")
    if not changes:
        raise HTTPException(422, "no changes supplied")
    # Re‑compute derived fields based on final times
    late = compute_late_minutes(pi, emp["shift_start"]) if pi else 0
    work = compute_work_hours(pi, po) if pi and po else None
    overtime = compute_overtime(po, emp["shift_end"], date) if po else 0
    half = work < 4.5 if work is not None else False
    # Build history entry
    history_entry = {
        "at": dt_to_epoch(datetime.now(tz=IST)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {}
    }
    for k, v in changes.items():
        # Convert datetime objects back to epoch ms for audit trail
        if isinstance(v["to"], datetime):
            v["to"] = dt_to_epoch(v["to"])
        if isinstance(v["from"], datetime):
            v["from"] = dt_to_epoch(v["from"])
        history_entry["changes"][k] = v
    # Prepare update document
    update_doc = {
        "$set": {
            "status": body.status or record["status"],
            "punch_in": pi,
            "punch_out": po,
            "late_minutes": late,
            "work_hours": work,
            "overtime_minutes": overtime,
            "half_day": half,
        },
        "$push": {"history": history_entry},
    }
    try:
        db.attendance_logs.update_one({"_id": record["_id"]}, update_doc)
    except DuplicateKeyError:
        raise HTTPException(409, "concurrent modification conflict")
    record.update(update_doc["$set"])
    record["history"].append(history_entry)
    record["id"] = str(record.pop("_id"))
    return record

# ---------------------------------------------------------------------------
# Analytics endpoints (using aggregation pipelines)
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    # Validate month format
    try:
        month_start = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        raise HTTPException(422, "Invalid month format")
    # Compute last day of month
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(days=1)
    # Employee existence
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    # Working days calculation (Mon‑Fri, after joined_on)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}},
        {"$addFields": {"dateObj": {"$dateFromString": {"dateString": "$date"}}}},
        {"$addFields": {"is_workday": {"$in": [{"$dayOfWeek": "$dateObj"}, [2,3,4,5,6]]}}},
        {"$match": {"is_workday": True}},
        {"$group": {
            "_id": None,
            "working_days": {"$sum": 1},
            "present_days": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT","WFH","ON_DUTY"]]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
        {"$project": {
            "_id": 0,
            "emp_code": emp_code,
            "month": month,
            "working_days": "$working_days",
            "present_days": "$present_days",
            "leave_days": "$leave_days",
            "late_count": "$late_count",
            "total_late_minutes": "$total_late_minutes",
            "total_overtime_minutes": "$total_overtime_minutes",
            "attendance_pct": {"$cond": [{"$eq": ["$working_days", 0]}, None, {"$round": [{"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]}, 2]}]}
        }},
    ]
    result = list(db.attendance_logs.aggregate(pipeline))
    if not result:
        # No logs for the month – still return zeros based on working days
        work_days = 0
        # Compute working days purely from calendar & joined_on
        cur = month_start
        while cur <= month_end:
            if cur.weekday() < 5 and cur.isoformat() >= emp["joined_on"]:
                work_days += 1
            cur += timedelta(days=1)
        return {
            "emp_code": emp_code,
            "month": month,
            "working_days": work_days,
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
            "attendance_pct": None if work_days == 0 else 0,
        }
    return result[0]

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    # month validation same as above
    try:
        month_start = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        raise HTTPException(422, "Invalid month format")
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(days=1)
    match_stage = {"date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}
    if department:
        match_stage["department"] = department
    pipeline = [
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": match_stage},
        {"$addFields": {"dateObj": {"$dateFromString": {"dateString": "$date"}}}},
        {"$addFields": {"is_workday": {"$in": [{"$dayOfWeek": "$dateObj"}, [2,3,4,5,6]]}}},
        {"$group": {
            "_id": "$emp.department",
            "headcount": {"$addToSet": {"emp_code": "$emp.emp_code", "joined_on": "$emp.joined_on"}},
            "present_days": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT","WFH","ON_DUTY"]]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", ["PRESENT","WFH","ON_DUTY"]]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", ["PRESENT","WFH","ON_DUTY"]]}, {"$ne": ["$work_hours", None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "department": "$_id",
            "headcount": {"$size": {"$filter": {"input": "$headcount", "as": "e", "cond": {"$lte": ["$$e.joined_on", month_end.isoformat()]}}}},
            "present_days": 1,
            "avg_work_hours": {"$cond": [{"$eq": ["$work_hours_count", 0]}, None, {"$round": [{"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2]}]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
        {"$group": {"_id": None, "month": {"$first": month}, "items": {"$push": "$$ROOT"}}},
        {"$project": {"_id": 0, "month": 1, "items": 1}}
    ]
    result = list(db.attendance_logs.aggregate(pipeline))
    if not result:
        return {"month": month, "items": []}
    return result[0]

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    if limit < 1 or limit > 50:
        raise HTTPException(422, "limit out of range")
    try:
        month_start = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        raise HTTPException(422, "Invalid month format")
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(days=1)
    match_stage = {"date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}, "late_minutes": {"$gt": 0}}
    if department:
        match_stage["department"] = department
    pipeline = [
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": match_stage},
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$emp.name"},
            "department": {"$first": "$emp.department"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    # Compute standard competition ranking in Python (as MongoDB doesn't have it directly)
    ranked = []
    prev_minutes = None
    rank = 0
    skipped = 0
    for i, r in enumerate(rows, start=1):
        if r["total_late_minutes"] != prev_minutes:
            rank = i
            prev_minutes = r["total_late_minutes"]
        r["rank"] = rank
        ranked.append(r)
    # Apply limit after ranking (including ties)
    filtered = [r for r in ranked if r["rank"] <= limit]
    return {"month": month, "items": filtered}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str, to_date: str):
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
        to_dt = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(422, "Invalid date format")
    if to_dt < from_dt:
        raise HTTPException(422, "to cannot be earlier than from")
    # Validate range <= 92 days
    if (to_dt - from_dt).days > 92:
        raise HTTPException(422, "Date range exceeds 92 days")
    # Verify department exists
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")
    pipeline = [
        {"$match": {"department": department, "date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {"from": "employees", "let": {"dateStr": "$date"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$dateStr"]}]}}}, {"$project": {"_id": 0}}], "as": "employees"}},
        {"$addFields": {"dateObj": {"$dateFromString": {"dateString": "$date"}}}},
        {"$group": {"_id": "$date", "present": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT","WFH","ON_DUTY"]]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}}, "late": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}}},
        {"$project": {"date": "$_id", "present": 1, "late": 1, "_id": 0}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [from_date, to_date]}, "partitionByFields": []}},
        {"$addFields": {"is_working_day": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2,3,4,5,6]]}, "headcount": {"$size": "$employees"}, "present_count": "$present", "late_count": "$late"}},
        {"$addFields": {"attendance_rate": {"$cond": [{"$and": [{"$eq": ["$is_working_day", true]}, {"$gt": ["$headcount", 0]}]}, {"$round": [{"$divide": ["$present", "$headcount"]}, 4]}, null]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": ["-6", "current"]}, "output": {"$round": ["$value", 4]}}}}},
        {"$project": {"date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}}
    ]
    # Due to complexity, we fallback to a simpler Python implementation later if needed.
    rows = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": rows}

# ---------------------------------------------------------------------------
# Admin explain endpoint
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, **params):
    # Map endpoint name to the exact query builder used above
    # For brevity, we implement only a subset (attendance_list and employee_monthly)
    if endpoint == "attendance_list":
        q = {}
        if "emp_code" in params:
            q["emp_code"] = params["emp_code"]
        if "date_from" in params or "date_to" in params:
            q["date"] = {}
            if "date_from" in params:
                q["date"]["$gte"] = params["date_from"]
            if "date_to" in params:
                q["date"]["$lte"] = params["date_to"]
        if "status" in params:
            q["status"] = params["status"]
        explain = db.attendance_logs.find(q).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    elif endpoint == "employee_monthly":
        # reuse the same pipeline from employee_monthly (without $project for brevity)
        month = params.get("month")
        emp_code = params.get("emp_code")
        try:
            month_start = datetime.strptime(month, "%Y-%m").date()
        except Exception:
            raise HTTPException(422, "Invalid month")
        next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        month_end = next_month - timedelta(days=1)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}},
            {"$addFields": {"dateObj": {"$dateFromString": {"dateString": "$date"}}}},
            {"$addFields": {"is_workday": {"$in": [{"$dayOfWeek": "$dateObj"}, [2,3,4,5,6]]}}},
            {"$match": {"is_workday": True}},
            {"$group": {"_id": None, "working_days": {"$sum": 1}}},
        ]
        explain = db.attendance_logs.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    else:
        raise HTTPException(400, "Explain not implemented for this endpoint")
# --------------------------------------------------------------------------- #
