"""iter133 — Team Score module.

COMPLETELY ISOLATED from the hiring pipeline. Uses ONLY the two
collections:
    ts_rounds      — Team Rounds (round_name, total_score)
    ts_employees   — Team Employee Scores (full employee profile +
                     round_scores dict)

Zero reads/writes to: pipeline_data, naukri_applies,
bb_applicant_updates, bb_rounds, bb_job_roles, job_titles_master, or
any hiring collection.

All endpoints under /api/team-score/* require admin auth.
"""

import csv
import hashlib
import io
import logging
import os
import re
from datetime import datetime, date, timezone
from typing import Any, Optional

from bson import ObjectId
from fastapi import APIRouter, Body, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

_logger = logging.getLogger("team_score")

# Wired by team_score.attach(app, db, require_auth) from server.py at
# startup. Keeps this module decoupled from server.py module-level state.
_db: Optional[AsyncIOMotorDatabase] = None
_require_auth = None

ts_router = APIRouter(prefix="/api/team-score", tags=["team-score"])


# ─────────────────────── Helpers ─────────────────────────────────────────

def _oid(s: str) -> ObjectId:
    try:
        return ObjectId(s)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_oid(doc: dict) -> dict:
    if not doc:
        return doc
    if "_id" in doc:
        doc["id"] = str(doc.pop("_id"))
    return doc


# iter135 — Joining-date helpers.
# DB canonical: "yyyy-mm-dd". Import: accept "dd-mm-yyyy" or "yyyy-mm-dd"
# (with "-", "/", "." separators) and Excel datetime/date objects.
# Display/Export: "dd-mm-yyyy".

_DATE_RE_YMD = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_DATE_RE_DMY = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$")


def _normalize_joining_date(value) -> str:
    """Return canonical 'yyyy-mm-dd' string, or '' if value can't be parsed.
    Pass-through for already-canonical strings; handles dd-mm-yyyy variants
    and Python datetime/date instances (Excel reads dates as datetime)."""
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if not s:
        return ""
    # Strip an accidental time suffix (e.g. "2026-06-01T00:00:00").
    if "T" in s:
        s = s.split("T", 1)[0]
    if " " in s:
        s = s.split(" ", 1)[0]
    m = _DATE_RE_YMD.match(s)
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        return f"{y}-{mo:02d}-{d:02d}"
    m = _DATE_RE_DMY.match(s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        return f"{y}-{mo:02d}-{d:02d}"
    return s  # unknown format — store verbatim (don't drop user data)


def _format_joining_date_display(value) -> str:
    """Convert stored 'yyyy-mm-dd' to display 'dd-mm-yyyy'.
    Strips any accidental time suffix. Returns '' for empty input."""
    if value in (None, ""):
        return ""
    s = str(value).strip()
    if "T" in s:
        s = s.split("T", 1)[0]
    if " " in s:
        s = s.split(" ", 1)[0]
    m = _DATE_RE_YMD.match(s)
    if m:
        return f"{int(m.group(3)):02d}-{int(m.group(2)):02d}-{m.group(1)}"
    m = _DATE_RE_DMY.match(s)
    if m:
        return f"{int(m.group(1)):02d}-{int(m.group(2)):02d}-{m.group(3)}"
    return s


# ─────────────────────── Pydantic models ─────────────────────────────────

class RoundIn(BaseModel):
    round_name: str
    total_score: float


class EmployeeIn(BaseModel):
    name: str
    email: Optional[str] = ""
    linkedin_id: Optional[str] = ""
    role: Optional[str] = ""
    joining_date: Optional[str] = ""
    college: Optional[str] = ""
    nirf_rank: Optional[str] = ""
    degree: Optional[str] = ""
    passing_year: Optional[str] = ""
    round_scores: Optional[dict] = None  # {round_name: score}
    employee_status: Optional[str] = "active"  # active | inactive


# ─────────────────────── Rounds CRUD ─────────────────────────────────────

@ts_router.get("/rounds")
async def list_rounds(request: Request):
    await _require_auth(request)
    rows = await _db.ts_rounds.find({}).sort("round_name", 1).to_list(None)
    return {"rounds": [_strip_oid(r) for r in rows]}


@ts_router.post("/rounds")
async def create_round(body: RoundIn, request: Request):
    await _require_auth(request)
    name = (body.round_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Round name required")
    existing = await _db.ts_rounds.find_one(
        {"round_name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}}
    )
    if existing:
        raise HTTPException(status_code=409, detail="Round already exists")
    doc = {
        "round_name": name,
        "total_score": float(body.total_score or 0),
        "created_at": _now(),
        "updated_at": _now(),
    }
    res = await _db.ts_rounds.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc


@ts_router.put("/rounds/{round_id}")
async def update_round(round_id: str, body: RoundIn, request: Request):
    await _require_auth(request)
    doc = await _db.ts_rounds.find_one({"_id": _oid(round_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Round not found")
    old_name = doc.get("round_name")
    new_name = (body.round_name or "").strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="Round name required")
    await _db.ts_rounds.update_one(
        {"_id": _oid(round_id)},
        {"$set": {
            "round_name": new_name,
            "total_score": float(body.total_score or 0),
            "updated_at": _now(),
        }},
    )
    # If renamed → rename the key in every employee's round_scores
    if old_name and new_name != old_name:
        async for emp in _db.ts_employees.find(
            {f"round_scores.{old_name}": {"$exists": True}}
        ):
            scores = emp.get("round_scores", {}) or {}
            scores[new_name] = scores.pop(old_name, None)
            await _db.ts_employees.update_one(
                {"_id": emp["_id"]}, {"$set": {"round_scores": scores}}
            )
    return {"success": True}


@ts_router.delete("/rounds/{round_id}")
async def delete_round(round_id: str, request: Request):
    await _require_auth(request)
    doc = await _db.ts_rounds.find_one({"_id": _oid(round_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Round not found")
    name = doc.get("round_name")
    await _db.ts_rounds.delete_one({"_id": _oid(round_id)})
    # Also remove the matching key from every employee's round_scores.
    if name:
        await _db.ts_employees.update_many(
            {f"round_scores.{name}": {"$exists": True}},
            {"$unset": {f"round_scores.{name}": ""}},
        )
    return {"success": True}


# ─────────────────────── Employees CRUD + filter ─────────────────────────

async def _ensure_round_exists(round_name: str, total_score: float = 0) -> str:
    """Create a round on the fly if missing. Returns the canonical name
    (case-preserving)."""
    name = (round_name or "").strip()
    if not name:
        return ""
    existing = await _db.ts_rounds.find_one(
        {"round_name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}}
    )
    if existing:
        return existing["round_name"]
    await _db.ts_rounds.insert_one({
        "round_name": name,
        "total_score": float(total_score or 0),
        "created_at": _now(),
        "updated_at": _now(),
    })
    return name


# iter139 — Email-based deduplication for Team Score employees.
# Email is the unique business key. On every insert / import / manual edit,
# we COMPLETELY REPLACE any existing record(s) sharing the same email
# (case-insensitive). Pre-existing duplicates are consolidated in the
# same pass: a single email yields exactly ONE surviving doc — the new
# incoming one. Empty emails skip dedup (insert normally).
async def _replace_by_email(new_doc: dict, exclude_id=None) -> dict:
    """Apply email-based full-replace.

    - If `new_doc['email']` is empty → just insert (no dedup possible).
    - If `exclude_id` is given (manual edit path), delete every OTHER
      doc that shares the email so the target row remains the sole
      survivor.
    - Otherwise delete ALL docs that share the email and insert the new
      one fresh.

    Returns the persisted doc with `_id` populated.
    """
    email = (new_doc.get("email") or "").strip()
    if not email:
        # No email → behave as a plain insert (existing path used
        # `(name, email)` upsert for emptyemail rows in import; we keep
        # the same neutral behaviour here to avoid silent merges).
        res = await _db.ts_employees.insert_one(new_doc)
        new_doc["_id"] = res.inserted_id
        return new_doc

    # Case-insensitive match — escape regex metacharacters in the user-
    # supplied email so e.g. "a+b@x.io" doesn't blow up the pattern.
    email_filter = {
        "email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}
    }
    if exclude_id is not None:
        # Manual edit path: target row stays; everything else with the
        # same email is removed to consolidate.
        await _db.ts_employees.delete_many(
            {"$and": [email_filter, {"_id": {"$ne": exclude_id}}]}
        )
        return new_doc  # caller has already applied $set on the target

    # Fresh insert / import path: wipe ALL rows with this email, insert
    # the new doc as the lone survivor.
    await _db.ts_employees.delete_many(email_filter)
    res = await _db.ts_employees.insert_one(new_doc)
    new_doc["_id"] = res.inserted_id
    return new_doc


def _build_filter_query(params: dict) -> dict:
    """Map UI filter params to a Mongo filter against ts_employees."""
    q = {}
    if params.get("employee_status"):
        q["employee_status"] = params["employee_status"]
    team = (params.get("team") or "").strip()
    if team:
        q["team"] = team
    for f in ("name", "email", "role"):
        v = (params.get(f) or "").strip()
        if v:
            q[f] = {"$regex": re.escape(v), "$options": "i"}
    nirf = (params.get("nirf_rank") or "").strip()
    if nirf:
        q["nirf_rank"] = nirf
    # iter158 — multi-select employee filter. `employee_ids` is a comma-separated
    # list of `_id` hex strings; the resulting filter is an OR over the selected
    # employees and AND over the remaining field filters (per the spec).
    ids_csv = (params.get("employee_ids") or "").strip()
    if ids_csv:
        from bson import ObjectId
        oids = []
        for s in ids_csv.split(","):
            s = s.strip()
            if not s:
                continue
            try:
                oids.append(ObjectId(s))
            except Exception:  # noqa: BLE001 — ignore malformed IDs rather than 500
                continue
        if oids:
            q["_id"] = {"$in": oids}
    return q


@ts_router.get("/employees")
async def list_employees(
    request: Request,
    employee_status: Optional[str] = None,
    team: Optional[str] = None,
    name: Optional[str] = None,
    email: Optional[str] = None,
    role: Optional[str] = None,
    nirf_rank: Optional[str] = None,
    employee_ids: Optional[str] = None,
):
    await _require_auth(request)
    q = _build_filter_query({
        "employee_status": employee_status, "team": team,
        "name": name, "email": email, "role": role, "nirf_rank": nirf_rank,
        "employee_ids": employee_ids,
    })
    rows = await _db.ts_employees.find(q).to_list(None)
    rows.sort(key=lambda r: (
        (r.get("employee_status") or "active").lower() != "active",
        (r.get("team") or "").lower(),
        (r.get("name") or "").lower(),
    ))
    total = await _db.ts_employees.count_documents({})
    return {"employees": [_strip_oid(r) for r in rows],
            "filtered_count": len(rows), "total_count": total}


@ts_router.get("/filters")
async def filter_options(request: Request):
    """DISTINCT values pulled exclusively from ts_employees."""
    await _require_auth(request)

    async def _d(field):
        vals = await _db.ts_employees.distinct(field)
        return sorted([str(v) for v in vals if v])
    return {
        "employee_status": ["active", "inactive"],
        "team": await _d("team"),
        "name": await _d("name"),
        "email": await _d("email"),
        "role": await _d("role"),
        "nirf_rank": await _d("nirf_rank"),
    }


@ts_router.post("/employees")
async def create_employee(body: EmployeeIn, request: Request):
    await _require_auth(request)
    if not (body.name or "").strip():
        raise HTTPException(status_code=400, detail="Name required")
    scores = body.round_scores or {}
    # Auto-create missing rounds (rare for manual create; harmless).
    for rn in list(scores.keys()):
        await _ensure_round_exists(rn)
    doc = {
        "employee_status": (body.employee_status or "active").lower(),
        "name": body.name.strip(),
        "email": (body.email or "").strip(),
        "linkedin_id": (body.linkedin_id or "").strip(),
        "role": (body.role or "").strip(),
        "joining_date": _normalize_joining_date(body.joining_date),
        "college": (body.college or "").strip(),
        "nirf_rank": (body.nirf_rank or "").strip(),
        "degree": (body.degree or "").strip(),
        "passing_year": (body.passing_year or "").strip(),
        "round_scores": {k: float(v) for k, v in scores.items() if v not in (None, "")},
        "created_at": _now(),
        "updated_at": _now(),
    }
    # iter139 — email-based full replace (case-insensitive).
    persisted = await _replace_by_email(doc)
    persisted["id"] = str(persisted.pop("_id"))
    return persisted


@ts_router.put("/employees/{emp_id}")
async def update_employee(emp_id: str, body: EmployeeIn, request: Request):
    await _require_auth(request)
    exists = await _db.ts_employees.find_one({"_id": _oid(emp_id)})
    if not exists:
        raise HTTPException(status_code=404, detail="Employee not found")
    scores = body.round_scores or {}
    for rn in list(scores.keys()):
        await _ensure_round_exists(rn)
    await _db.ts_employees.update_one(
        {"_id": _oid(emp_id)},
        {"$set": {
            "name": body.name.strip(),
            "email": (body.email or "").strip(),
            "linkedin_id": (body.linkedin_id or "").strip(),
            "role": (body.role or "").strip(),
            "joining_date": _normalize_joining_date(body.joining_date),
            "college": (body.college or "").strip(),
            "nirf_rank": (body.nirf_rank or "").strip(),
            "degree": (body.degree or "").strip(),
            "passing_year": (body.passing_year or "").strip(),
            "round_scores": {k: float(v) for k, v in scores.items() if v not in (None, "")},
            "updated_at": _now(),
        }},
    )
    # iter139 — consolidate any OTHER rows sharing this email so the
    # edited row remains the sole survivor (case-insensitive).
    await _replace_by_email(
        {"email": (body.email or "").strip()},
        exclude_id=_oid(emp_id),
    )
    return {"success": True}


@ts_router.post("/employees/bulk-delete")
async def bulk_delete_employees(request: Request, body: dict = Body(...)):  # noqa: B008
    """Delete multiple Team Score employee rows by `_id`. Narrowly filtered by
    an explicit ObjectId list from an authenticated admin confirmation
    dialog — never a collection-wide delete. Returns the deleted count so
    the UI can report partial success honestly.

    Request body: ``{"ids": ["<objectid hex>", ...]}``. Malformed hex is
    silently dropped (never 500). Only rows in `ts_employees` are touched;
    the employee's HRMS account, round catalogue and unrelated collections
    are untouched (Team Score stores its own documents)."""
    await _require_auth(request)
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="`ids` must be a non-empty list")
    from bson import ObjectId
    oids = []
    for s in ids:
        try:
            oids.append(ObjectId(str(s).strip()))
        except Exception:  # noqa: BLE001
            continue
    if not oids:
        raise HTTPException(status_code=400, detail="No valid employee IDs supplied")
    res = await _db.ts_employees.delete_many({"_id": {"$in": oids}})
    return {"success": True, "requested": len(ids), "deleted": res.deleted_count,
            "missing": len(ids) - res.deleted_count}


@ts_router.delete("/employees/{emp_id}")
async def delete_employee(emp_id: str, request: Request):
    await _require_auth(request)
    r = await _db.ts_employees.delete_one({"_id": _oid(emp_id)})
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Employee not found")
    return {"success": True}


@ts_router.post("/employees/{emp_id}/activate")
async def activate_employee(emp_id: str, request: Request):
    await _require_auth(request)
    r = await _db.ts_employees.update_one(
        {"_id": _oid(emp_id)},
        {"$set": {"employee_status": "active", "updated_at": _now()}},
    )
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Employee not found")
    return {"success": True, "employee_status": "active"}


@ts_router.post("/employees/{emp_id}/deactivate")
async def deactivate_employee(emp_id: str, request: Request):
    await _require_auth(request)
    r = await _db.ts_employees.update_one(
        {"_id": _oid(emp_id)},
        {"$set": {"employee_status": "inactive", "updated_at": _now()}},
    )
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Employee not found")
    return {"success": True, "employee_status": "inactive"}


# ─────────────────────── Export ──────────────────────────────────────────

async def _collect_export_rows(filter_q: dict):
    rounds = await _db.ts_rounds.find({}).sort("round_name", 1).to_list(None)
    # iter134 — total_score may now be NULL (rounds created on import
    # without a header total). Carry the raw value through so the
    # header column renders "Name(NULL)" instead of guessing 0.
    round_headers = [(r["round_name"], r.get("total_score")) for r in rounds]
    emps = await _db.ts_employees.find(filter_q).to_list(None)
    # Active first, separator row, then inactive — per spec.
    actives = [e for e in emps if (e.get("employee_status") or "active").lower() == "active"]
    inactives = [e for e in emps if (e.get("employee_status") or "active").lower() == "inactive"]
    actives.sort(key=lambda e: (e.get("name") or "").lower())
    inactives.sort(key=lambda e: (e.get("name") or "").lower())
    base_cols = ["Name", "Email ID", "LinkedIn ID", "Role", "Joining Date",
                 "College", "NIRF Rank", "Degree", "Passing Year"]

    def _fmt_total(ts):
        # iter135 — Skip the trailing "(...)" suffix when total is missing
        # so round-trip import preserves the round name verbatim.
        if ts in (None, ""):
            return None
        try:
            f = float(ts)
            if f <= 0:
                return None
            return str(int(f)) if f.is_integer() else str(f)
        except (TypeError, ValueError):
            return str(ts)

    def _header_for(rn, ts):
        t = _fmt_total(ts)
        return rn if t is None else f"{rn}({t})"

    headers = base_cols + [_header_for(rn, ts) for rn, ts in round_headers]

    def _fmt_cell(score, total):
        """iter134 — Export cells now mirror the Team Score table:
        `score/total (pct%)`, raw score when total missing,
        and "-" when employee has no score."""
        if score in (None, ""):
            return "-"
        try:
            s = float(score)
        except (TypeError, ValueError):
            return str(score)
        s_str = str(int(s)) if s.is_integer() else str(s)
        if total in (None, "") or (isinstance(total, (int, float)) and float(total) <= 0):
            return s_str
        try:
            t = float(total)
            pct = (s / t) * 100
            t_str = str(int(t)) if t.is_integer() else str(t)
            return f"{s_str}/{t_str} ({pct:.2f}%)"
        except (TypeError, ValueError, ZeroDivisionError):
            return s_str

    def emp_row(e):
        scores = e.get("round_scores") or {}
        row = [
            e.get("name") or "",
            e.get("email") or "",
            e.get("linkedin_id") or "",
            e.get("role") or "",
            _format_joining_date_display(e.get("joining_date") or ""),
            e.get("college") or "",
            e.get("nirf_rank") or "",
            e.get("degree") or "",
            e.get("passing_year") or "",
        ]
        for rn, ts in round_headers:
            row.append(_fmt_cell(scores.get(rn), ts))
        return row

    rows = [emp_row(e) for e in actives]
    if inactives:
        sep = ["INACTIVE EMPLOYEES"] + [""] * (len(headers) - 1)
        rows.append(sep)
        rows.extend([emp_row(e) for e in inactives])
    return headers, rows


@ts_router.get("/export")
async def export_team_scores(
    request: Request,
    fmt: str = Query("csv", regex="^(csv|xlsx)$"),
    employee_status: Optional[str] = None,
    name: Optional[str] = None,
    email: Optional[str] = None,
    role: Optional[str] = None,
    nirf_rank: Optional[str] = None,
):
    await _require_auth(request)
    q = _build_filter_query({
        "employee_status": employee_status, "name": name, "email": email,
        "role": role, "nirf_rank": nirf_rank,
    })
    headers, rows = await _collect_export_rows(q)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(headers)
        w.writerows(rows)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="team_scores.csv"'},
        )
    # xlsx
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Team Scores"
    ws.append(headers)
    for r in rows:
        ws.append(r)
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return StreamingResponse(
        out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="team_scores.xlsx"'},
    )


# ─────────────────────── Import ──────────────────────────────────────────

# iter135 — Round columns are NOT parsed any more. After the standard
# employee columns, every remaining header is treated as the complete
# Team Round Name exactly as it appears in the file (no bracket
# processing, no total-score extraction).
_BASE_COL_MAP = {
    "name": "name", "employee name": "name", "full name": "name",
    "email": "email", "email id": "email", "email_id": "email",
    "email id/ teams": "email", "email id/teams": "email",
    "official email/ teams id": "email_teams_id",
    "official email/teams id": "email_teams_id",
    "linkedin": "linkedin_id", "linkedin id": "linkedin_id", "linkedin_id": "linkedin_id",
    "linkedin url": "linkedin_url", "linkedin profile": "linkedin_url",
    "role": "role", "designation": "role",
    "joining date": "joining_date", "joining_date": "joining_date", "doj": "joining_date",
    "relieving date": "relieving_date", "relieving_date": "relieving_date",
    "s.no": "s_no", "sno": "s_no", "s_no": "s_no", "serial no": "s_no", "serial_no": "s_no",
    "team": "team", "department": "team",
    "employment status": "employment_status", "employment_status": "employment_status",
    "college": "college", "institute": "college",
    "nirf rank": "nirf_rank", "nirf_rank": "nirf_rank", "nirf": "nirf_rank",
    "nirf (2025 engineering)": "nirf_rank", "nirf (engineering)": "nirf_rank",
    "degree": "degree", "qualification": "degree",
    "passing year": "passing_year", "passing_year": "passing_year", "year of passing": "passing_year",
}

# iter156 — Header auto-detection: the Consolidated workbook puts a merged
# group-header on row 1 and the actual column labels on row 2. Scan up to
# `_HEADER_SCAN_MAX` leading rows and pick the first one that has at least
# `_HEADER_MIN_NONEMPTY` non-empty string cells AND at least one recognised
# base column, so legacy uploads (header on row 1) continue to work.
_HEADER_SCAN_MAX = 10
_HEADER_MIN_NONEMPTY = 3


def _header_key(h: Any) -> str:
    return re.sub(r"\s+", " ", str(h or "").strip()).lower()


def _excel_letter(col_index_1based: int) -> str:
    """Convert a 1-based column index into the Excel letter (A, B, ..., Z,
    AA, AB, ..., DJ, ...). We avoid pulling `openpyxl.utils.get_column_letter`
    into this helper so the function stays usable from CSV import paths too."""
    n = col_index_1based
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _find_header_row(rows: list) -> int:
    """Return the 0-based index of the row that contains the column headers.

    A row qualifies when it has ≥ `_HEADER_MIN_NONEMPTY` non-empty string
    cells and at least one cell matches `_BASE_COL_MAP` (so the "Name" column
    can be located for row ingestion). Falls back to row 0 if nothing obvious
    is found so the legacy single-header CSV path keeps working."""
    scan = min(_HEADER_SCAN_MAX, len(rows))
    for i in range(scan):
        non_empty = [c for c in rows[i] if str(c or "").strip()]
        if len(non_empty) < _HEADER_MIN_NONEMPTY:
            continue
        if any(_header_key(c) in _BASE_COL_MAP for c in rows[i]):
            return i
    return 0


def _cell_value(v: Any) -> Any:
    """Return a JSON-safe representation of an Excel cell.

    - `None` and empty strings stay as "" so blanks never collapse to 0.
    - Datetime/date → ISO 'yyyy-mm-dd' strings (Excel date serials are
      already converted by openpyxl when the cell is typed as a date).
    - Integer-valued floats become plain ints so ``0.0`` is persisted as ``0``.
    - Everything else round-trips as-is."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        return v.strip() if v.strip() != v else v
    return v


def _is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _normalize_status(v: Any) -> str:
    """Map the Excel 'Employment Status' column to the canonical
    `employee_status` ("active" | "inactive"). Preserves the original
    verbatim string on the doc via `employment_status` for display."""
    s = str(v or "").strip().lower()
    if s in ("inactive", "relieved", "ex", "left", "exited"):
        return "inactive"
    return "active"


def _import_key(team: str, name: str, joining_date: str, s_no: Any) -> str:
    """Stable composite identifier for duplicate detection on re-import.
    Based on (team, lowercased-name, joining_date, s_no) so the same
    employee row maps to the same document across reruns even when
    email is missing or shared. Returns '' if `name` is blank — unnamed
    rows are routed to a separate review collection and never match by
    this key."""
    nm = re.sub(r"\s+", " ", str(name or "").strip().lower())
    if not nm:
        return ""
    sn = str(s_no or "").strip()
    jd = (joining_date or "").strip()
    tm = (team or "").strip().lower()
    base = f"{tm}|{nm}|{jd}|{sn}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()  # noqa: S324 — stable composite key, not a password


def _row_has_inactive_marker(row) -> bool:
    return any(
        isinstance(v, str) and "inactive employees" in v.strip().lower()
        for v in row
    )


@ts_router.post("/import")
async def import_team_scores(
    request: Request,
    file: UploadFile = File(...),
    preview: int = Query(0, ge=0, le=1),
):
    """Import the Consolidated workbook.

    Supports CSV, .xls and .xlsx. Auto-detects the header row (skips the
    leading merged-group row on the Consolidated sheet). Every one of the
    original columns is preserved verbatim on each employee doc under
    `raw_columns`, so blank stays blank, zero stays zero, dates keep their
    original representation and duplicated headers (e.g. "Score /10" ×5)
    are not collapsed. Rows with a blank `Name` are routed to the
    `ts_employees_unnamed` collection for admin review instead of being
    silently discarded or getting a fabricated name.

    Query param `preview=1` runs in dry-run mode: nothing is written to
    `ts_employees` / `ts_rounds` / `ts_employees_unnamed`. The response
    contains the header mapping, row counts by team/status, the 2
    unnamed rows and the first 10 named rows so admins can validate the
    mapping before committing.
    """
    await _require_auth(request)
    raw = await file.read()
    filename = (file.filename or "").lower()
    rows: list = []
    if filename.endswith(".csv"):
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = [r for r in reader]
    elif filename.endswith(".xlsx") or filename.endswith(".xls"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), data_only=True)
        ws = wb.active
        for r in ws.iter_rows(values_only=True):
            rows.append(list(r))
    else:
        raise HTTPException(status_code=400, detail="Use .csv or .xlsx")

    if not rows:
        raise HTTPException(status_code=400, detail="Empty file")

    # iter156 — Auto-detect header row (Consolidated workbook puts headers on row 2).
    header_row_idx = _find_header_row(rows)
    headers = [str(h or "").strip() for h in rows[header_row_idx]]
    body = rows[header_row_idx + 1:]

    # iter157 — Build the authoritative ordered column schema from row 2 of
    # the uploaded workbook. Excel column A → index 0 → position 1 in the
    # UI; Excel column DJ → index 113 → position 114. The frontend renders
    # the Team Score table STRICTLY against this schema so the display
    # order is guaranteed to match the Excel left-to-right sequence, even
    # when two columns share the same heading.
    column_schema = [
        {"col": i,
         "letter": _excel_letter(i + 1),
         "header": headers[i],
         "header_original": rows[header_row_idx][i],
         "column_id": f"c{i:03d}"}  # stable per-position identifier
        for i in range(len(headers))
    ]

    # Column classification: base-field columns vs round-score columns.
    base_idx: dict = {}
    round_idx: list = []  # list of (col_index, round_name_verbatim)
    base_dupes: list = []  # (header, col_index) when two headers map to the same base field
    unmapped_blank_headers: list = []  # col_indexes with blank header but data below
    for i, h in enumerate(headers):
        key = _header_key(h)
        if not h:
            unmapped_blank_headers.append(i)
            continue
        if key in _BASE_COL_MAP:
            fld = _BASE_COL_MAP[key]
            if fld in base_idx:
                # Already mapped → keep the first occurrence as the base
                # field and treat subsequent ones as additional "round"
                # columns so duplicated headers (e.g. "Role" twice) are
                # never silently dropped.
                base_dupes.append((h, i))
                round_idx.append((i, h.strip()))
            else:
                base_idx[fld] = i
            continue
        # iter135 — treat ENTIRE header as the round name. No bracket
        # parsing, no splitting, no total-score extraction.
        round_idx.append((i, h.strip()))

    if "name" not in base_idx:
        raise HTTPException(status_code=400, detail="`Name` column missing")

    # Walk body: classify into named vs unnamed, build docs, collect preview data.
    named_docs: list = []
    unnamed_docs: list = []
    status_from_marker = "active"
    separators = 0
    skipped_blank_rows = 0
    row_errors: list = []

    for row_offset, r in enumerate(body):
        if len(r) < len(headers):
            r = list(r) + [""] * (len(headers) - len(r))
        if all(_is_blank(v) for v in r):
            skipped_blank_rows += 1
            continue
        if _row_has_inactive_marker(r):
            status_from_marker = "inactive"
            separators += 1
            continue

        def _at(field):
            i = base_idx.get(field)
            if i is None:
                return ""
            v = r[i]
            return "" if v is None else str(v).strip() if isinstance(v, str) else v

        nm = _at("name")
        raw_columns = [
            {"col": idx, "header": headers[idx], "value": _cell_value(r[idx])}
            for idx in range(len(headers))
        ]
        team_val = str(_at("team") or "").strip()
        emp_stat_verbatim = str(_at("employment_status") or "").strip()
        employee_status = _normalize_status(emp_stat_verbatim) if emp_stat_verbatim else status_from_marker

        joining_norm = _normalize_joining_date(r[base_idx["joining_date"]]
                                              if "joining_date" in base_idx else "")
        relieving_norm = _normalize_joining_date(r[base_idx["relieving_date"]]
                                                 if "relieving_date" in base_idx else "")

        # Round scores: numeric cells only — non-numeric (e.g. date strings
        # in a "Date" column) are left in raw_columns verbatim and do NOT
        # get force-converted to zero. Blank also stays blank — key is
        # simply not present in `round_scores`.
        scores = {}
        for i, rn in round_idx:
            v = r[i]
            if _is_blank(v):
                continue
            try:
                scores[rn] = float(v)
            except (TypeError, ValueError):
                continue

        if not str(nm or "").strip():
            # iter156 — unnamed rows are kept for admin review.
            unnamed_docs.append({
                "source_row_index": header_row_idx + 1 + row_offset,  # 0-based position in original workbook
                "team": team_val,
                "employment_status_raw": emp_stat_verbatim,
                "employee_status": employee_status,
                "s_no": _at("s_no"),
                "joining_date": joining_norm,
                "relieving_date": relieving_norm,
                "role": str(_at("role") or "").strip(),
                "email": str(_at("email") or "").strip(),
                "raw_columns": raw_columns,
                "notes": "No employee name recorded in source workbook",
                "created_at": _now(),
                "updated_at": _now(),
            })
            continue

        email_val = str(_at("email") or "").strip()
        doc = {
            "team": team_val,
            "employment_status": emp_stat_verbatim,
            "employee_status": employee_status,
            "s_no": _at("s_no"),
            "name": str(nm).strip(),
            "joining_date": joining_norm,
            "relieving_date": relieving_norm,
            "role": str(_at("role") or "").strip(),
            "email": email_val,
            "email_teams_id": str(_at("email_teams_id") or "").strip(),
            "linkedin_id": str(_at("linkedin_id") or "").strip(),
            "linkedin_url": str(_at("linkedin_url") or "").strip(),
            "college": str(_at("college") or "").strip(),
            "nirf_rank": str(_at("nirf_rank") or "").strip(),
            "degree": str(_at("degree") or "").strip(),
            "passing_year": str(_at("passing_year") or "").strip(),
            "round_scores": scores,
            "raw_columns": raw_columns,
            "import_key": _import_key(team_val, str(nm), joining_norm, _at("s_no")),
            "created_at": _now(),
            "updated_at": _now(),
        }
        named_docs.append(doc)

    # Build counts summary (named only, by (team, employee_status)).
    from collections import Counter
    by_team_status = Counter((d["team"], d["employee_status"]) for d in named_docs)
    team_totals = Counter(d["team"] for d in named_docs)

    preview_payload = {
        "filename": file.filename,
        "header_row_index": header_row_idx,
        "headers": headers,
        "header_count": len(headers),
        "column_schema": column_schema,
        "base_columns_mapped": base_idx,
        "round_columns": [h for _, h in round_idx],
        "duplicate_base_headers": base_dupes,
        "unmapped_blank_header_columns": unmapped_blank_headers,
        "counts": {
            "named_rows": len(named_docs),
            "unnamed_rows": len(unnamed_docs),
            "blank_rows_skipped": skipped_blank_rows,
            "inactive_separator_markers": separators,
            "team_totals": dict(team_totals),
            "by_team_status": {f"{t}|{s}": n for (t, s), n in by_team_status.items()},
        },
        "sample_named": [{k: d.get(k) for k in ("team", "employment_status", "name",
                                                "email", "role", "joining_date")}
                         for d in named_docs[:10]],
        "unnamed_rows": [{"source_row_index": u["source_row_index"], "team": u["team"],
                          "employee_status": u["employee_status"], "role": u["role"],
                          "s_no": u["s_no"], "email": u["email"],
                          "notes": u["notes"]} for u in unnamed_docs],
        "row_errors": row_errors,
    }
    if preview:
        return {"preview": True, **preview_payload}

    # Commit path -------------------------------------------------------------
    # iter157 — Persist the authoritative Excel-order column schema so the
    # /schema endpoint can serve the exact same ordered list across requests
    # and after server restarts. The schema is keyed by `_id="current"`;
    # the previous version is preserved under `_id="history"` for audit.
    prev_schema = await _db.ts_column_schema.find_one({"_id": "current"})
    if prev_schema:
        await _db.ts_column_schema.update_one(
            {"_id": "current"},
            {"$set": {"column_schema": column_schema, "header_row_index": header_row_idx,
                      "header_count": len(headers), "source_filename": file.filename,
                      "updated_at": _now()}},
        )
        await _db.ts_column_schema.update_one(
            {"_id": f"history_{_now()}"},
            {"$set": {"column_schema": prev_schema.get("column_schema"),
                      "superseded_at": _now()}},
            upsert=True,
        )
    else:
        await _db.ts_column_schema.insert_one(
            {"_id": "current", "column_schema": column_schema,
             "header_row_index": header_row_idx, "header_count": len(headers),
             "source_filename": file.filename,
             "created_at": _now(), "updated_at": _now()},
        )

    # Build a single bulk_write across employees + rounds to keep remote Atlas
    # latency well under the 60s proxy ceiling. 183 rows × ~4 single-doc ops
    # was timing out; one `bulk_write` finishes in a couple of seconds.
    from pymongo import ReplaceOne, InsertOne, UpdateOne

    created_rounds: list = []
    existing_round_names = {r["round_name"].lower(): r for r in
                            await _db.ts_rounds.find({}).to_list(None)}
    new_round_docs: list = []
    for _, rn in round_idx:
        if rn.lower() not in existing_round_names:
            new_round_docs.append({"round_name": rn, "total_score": None,
                                   "created_at": _now(), "updated_at": _now()})
            existing_round_names[rn.lower()] = {"round_name": rn, "total_score": None}
            created_rounds.append(rn)
    if new_round_docs:
        await _db.ts_rounds.insert_many(new_round_docs)

    # iter143 — round totals for clamping (NULL/0 totals skip clamp).
    round_totals: dict = {
        r["round_name"]: (float(r["total_score"]) if r.get("total_score") not in (None, "") else None)
        for r in existing_round_names.values()
    }
    scores_clamped = 0

    # Named rows: dedupe by `import_key` using a single bulk upsert. Re-imports
    # replace the matching doc in place — no row multiplication, no orphaned
    # legacy copies. Preserve `created_at` on existing docs by pulling them in
    # one $in-query first.
    import_keys = [d["import_key"] for d in named_docs if d.get("import_key")]
    existing_by_key = {}
    if import_keys:
        async for r in _db.ts_employees.find({"import_key": {"$in": import_keys}},
                                             {"import_key": 1, "created_at": 1, "_id": 1}):
            existing_by_key[r["import_key"]] = r

    ops = []
    for doc in named_docs:
        for rn, sv in list(doc["round_scores"].items()):
            t = round_totals.get(rn)
            if t is not None and t > 0 and sv > t:
                doc["round_scores"][rn] = t
                scores_clamped += 1
        key = doc.get("import_key") or ""
        prior = existing_by_key.get(key)
        if prior:
            doc["created_at"] = prior.get("created_at", doc["created_at"])
        ops.append(ReplaceOne({"import_key": key}, doc, upsert=True))

    inserted = updated = 0
    if ops:
        res = await _db.ts_employees.bulk_write(ops, ordered=False)
        upserted_ids = getattr(res, "upserted_ids", {}) or {}
        inserted = len(upserted_ids)
        # `modified_count` reflects docs whose fields actually changed on a
        # second import; `matched_count - modified_count` would be unchanged
        # replacements. Expose "updated" as matched-but-not-upserted so a
        # re-run of the same file reports `updated == len(named_docs)`.
        updated = res.matched_count

    # Unnamed rows: soft-supersede any previously-captured review rows that
    # are NOT present in this upload, then upsert each current one by its
    # `source_row_index`. No collection-wide deletes; the `/unnamed`
    # endpoint filters out superseded docs so admins only see the latest
    # review set.
    unnamed_superseded = 0
    if unnamed_docs:
        current_indexes = [u["source_row_index"] for u in unnamed_docs]
        res = await _db.ts_employees_unnamed.update_many(
            {"source_row_index": {"$nin": current_indexes},
             "superseded_at": {"$exists": False}},
            {"$set": {"superseded_at": _now()}},
        )
        unnamed_superseded = res.modified_count
        un_ops = [
            UpdateOne({"source_row_index": u["source_row_index"]},
                      {"$set": u, "$unset": {"superseded_at": ""}}, upsert=True)
            for u in unnamed_docs
        ]
        await _db.ts_employees_unnamed.bulk_write(un_ops, ordered=False)

    return {
        "success": True,
        "inserted": inserted,
        "updated": updated,
        "separators": separators,
        "rounds_created": created_rounds,
        "scores_clamped": scores_clamped,
        "unnamed_rows_captured": len(unnamed_docs),
        "unnamed_rows_superseded": unnamed_superseded,
        "preview": preview_payload["counts"],
    }


@ts_router.get("/schema")
async def get_column_schema(request: Request):
    """Return the authoritative Excel-order column schema persisted by the
    most recent `/import` commit. The frontend renders the Team Score table
    STRICTLY against this ordered list — index 0 is Excel column A, index
    len-1 is the last column in the uploaded workbook. Duplicated header
    strings (e.g. "Score /10") stay as separate entries with unique
    `column_id` values so UI order can never collapse them."""
    await _require_auth(request)
    doc = await _db.ts_column_schema.find_one({"_id": "current"})
    if not doc:
        return {"column_schema": [], "header_count": 0, "header_row_index": None,
                "source_filename": None}
    return {
        "column_schema": doc.get("column_schema") or [],
        "header_count": doc.get("header_count"),
        "header_row_index": doc.get("header_row_index"),
        "source_filename": doc.get("source_filename"),
        "updated_at": doc.get("updated_at"),
    }


@ts_router.get("/unnamed")
async def list_unnamed_rows(request: Request):
    """Rows the latest import could not attach to a named employee because
    the `Name` column was blank. Kept here for admin review only — they
    never appear in the main table or in search."""
    await _require_auth(request)
    rows = await _db.ts_employees_unnamed.find(
        {"superseded_at": {"$exists": False}},
    ).sort("source_row_index", 1).to_list(None)
    return {"unnamed": [_strip_oid(r) for r in rows]}


# iter160 — Admin Import Review delete endpoints. Scoped STRICTLY to
# `ts_employees_unnamed` and only to the explicit ObjectId list sent by the
# admin confirmation dialog. Never touches `ts_employees`, `ts_rounds`,
# HRMS accounts or any unrelated collection.
@ts_router.delete("/unnamed/{unnamed_id}")
async def delete_unnamed_row(unnamed_id: str, request: Request):
    """Delete a single Admin Import Review row by its MongoDB `_id`."""
    await _require_auth(request)
    r = await _db.ts_employees_unnamed.delete_one({"_id": _oid(unnamed_id)})
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Import review row not found")
    return {"success": True}


@ts_router.post("/unnamed/bulk-delete")
async def bulk_delete_unnamed_rows(request: Request, body: dict = Body(...)):  # noqa: B008
    """Delete multiple Admin Import Review rows by `_id`. Mirrors the
    iter159 employee bulk-delete contract: honest `{requested, deleted,
    missing}` envelope, malformed hex silently dropped, empty / all-
    malformed → 400. Only `ts_employees_unnamed` is touched.
    """
    await _require_auth(request)
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="`ids` must be a non-empty list")
    from bson import ObjectId
    oids = []
    for s in ids:
        try:
            oids.append(ObjectId(str(s).strip()))
        except Exception:  # noqa: BLE001
            continue
    if not oids:
        raise HTTPException(status_code=400, detail="No valid import review IDs supplied")
    res = await _db.ts_employees_unnamed.delete_many({"_id": {"$in": oids}})
    return {"success": True, "requested": len(ids), "deleted": res.deleted_count,
            "missing": len(ids) - res.deleted_count}


@ts_router.get("/suggest")
async def suggest_employees(request: Request, q: str = Query("", max_length=200),
                            limit: int = Query(10, ge=1, le=25)):
    """Name/email autocomplete. Case-insensitive substring match over
    `name` and `email`. Each suggestion carries the employee's name,
    team and employment status so the dropdown can show context
    straight away."""
    await _require_auth(request)
    term = (q or "").strip()
    if not term:
        return {"suggestions": []}
    # Collapse whitespace inside the query but keep the escaped original
    # for the regex so e.g. "Kath  S" matches "Kathirvel S".
    pattern = re.escape(re.sub(r"\s+", " ", term))
    rx = {"$regex": pattern, "$options": "i"}
    rows = await _db.ts_employees.find(
        {"$or": [{"name": rx}, {"email": rx}, {"email_teams_id": rx}]},
    ).limit(limit).to_list(None)
    rows.sort(key=lambda r: (
        (r.get("employee_status") or "active").lower() != "active",
        (r.get("name") or "").lower(),
    ))
    return {"suggestions": [
        {"id": str(r["_id"]), "name": r.get("name") or "", "email": r.get("email") or "",
         "team": r.get("team") or "", "employee_status": r.get("employee_status") or "active",
         "role": r.get("role") or ""}
        for r in rows
    ]}


# ─────────────────────── Attach ──────────────────────────────────────────

def attach(app, db, require_auth):
    """Called once from server.startup_event. Wires the router and the
    db/auth handles into this module without creating import cycles."""
    global _db, _require_auth
    _db = db
    _require_auth = require_auth
    app.include_router(ts_router)
    _logger.info("Team Score module attached at /api/team-score")
