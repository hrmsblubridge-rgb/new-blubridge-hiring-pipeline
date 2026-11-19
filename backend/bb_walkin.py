"""iter163 — Manual OTP Verification Enhancement.

Two self-contained features mounted under `/api/bb/manual-otp/*`:

* Feature A — Undo OTP Verification
    Employee submits a correction request → top-level admin approves/rejects
    → audited reversal preserving the original verification record. After an
    approved reversal, the ORIGINAL OTP may be reused on the SAME booking and
    SAME schedule_version (per Q4.7). The OTP itself is never exposed in any
    response, log or export.

* Feature B — Schedule & Verify Walk-in
    Authorized employee confirms a shortlisted candidate's physical presence
    at the Chennai office; one atomic backend operation computes today's IST
    slot (10:00–17:00 in 30-minute steps), creates or converts the booking,
    generates + verifies an OTP internally and marks attendance VERIFIED with
    source `WALK_IN_AUTO_VERIFIED`. Walk-in attendance suppresses all three
    AI-calling purposes (booking_confirmation, interview_confirmation,
    missed_interview_followup) and the scheduling / OTP notification worker
    hooks.

STRICT SAFETY — development only. Writes must go to `hiring_ai_testing`;
`init_walkin` refuses any DB whose name contains 'hr_analytics' unless the
`WALKIN_ALLOW_PROD=1` env flag is set (not set in this build).

New collections (all additive, no rewrites of existing docs):
    bb_otp_reversal_requests    — correction requests (A)
    bb_verification_events      — immutable audit chain (both features)

New fields added to EXISTING docs (additive):
    bb_users.permissions         — dict of boolean capability flags
    bb_registrations.walkin      — walk-in metadata (set on walk-in booking)
    bb_registrations.otp_state   — 'active' | 'reversed' (gates reuse)
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field

log = logging.getLogger("bb_walkin")

walkin_router = APIRouter(prefix="/api/bb/manual-otp")

# ── Dependency injection (same pattern as bb_modules.init_bb) ─────────────
_db = None
_auth_fn = None


def init_walkin(db, auth_fn):
    """Mount the module against a database + auth resolver. Refuses to bind
    to a hr_analytics-shaped database unless WALKIN_ALLOW_PROD=1."""
    global _db, _auth_fn
    name = getattr(db, "name", "")
    if "hr_analytics" in (name or "") and os.environ.get("WALKIN_ALLOW_PROD") != "1":
        raise RuntimeError(
            "bb_walkin refuses to bind to a hr_analytics database without "
            "WALKIN_ALLOW_PROD=1 (section 3 safety invariant)"
        )
    _db = db
    _auth_fn = auth_fn
    log.info("bb_walkin bound to db=%s", name)


# ── Permission model (per-feature boolean flags on bb_users) ──────────────

TOP_LEVEL_ADMIN_FLAG = "can_approve_otp_reversal"
EMPLOYEE_REVERSAL_FLAG = "can_request_otp_reversal"
EMPLOYEE_WALKIN_FLAG = "can_schedule_walkin"


async def _principal_from_request(request: Request) -> dict:
    """Return the authenticated user doc + effective permission map.
    Role `admin` implies ALL permission flags (back-compat). Permissions on
    the user doc override. Deny-by-default for any flag not set."""
    username = await _auth_fn(request)
    if not username:
        raise HTTPException(status_code=401, detail="Authentication required")
    doc = await _db.bb_users.find_one(
        {"username": username},
        {"_id": 1, "username": 1, "role": 1, "permissions": 1},
    )
    if not doc:
        # Legacy path: when `verify_password` auto-seeds Admin User, the doc
        # may not exist on first call. Treat as the (sole) admin.
        doc = {"username": username, "role": "admin", "permissions": {}}
    perms = dict(doc.get("permissions") or {})
    is_admin = (doc.get("role") == "admin")
    effective = {
        TOP_LEVEL_ADMIN_FLAG: bool(perms.get(TOP_LEVEL_ADMIN_FLAG, is_admin)),
        EMPLOYEE_REVERSAL_FLAG: bool(perms.get(EMPLOYEE_REVERSAL_FLAG, is_admin)),
        EMPLOYEE_WALKIN_FLAG: bool(perms.get(EMPLOYEE_WALKIN_FLAG, is_admin)),
    }
    return {"username": username, "role": doc.get("role"), "perms": effective,
            "user_id": str(doc.get("_id")) if doc.get("_id") else username}


def _require(principal: dict, flag: str) -> None:
    """Deny-by-default 403 when the flag is missing. 404-leaks-existence
    only matters for tenant-scoped data — here every bb_user is a trusted
    employee, so 403 is correct."""
    if not principal["perms"].get(flag):
        raise HTTPException(status_code=403, detail=f"Missing permission: {flag}")


# ── Time calculation (Asia/Kolkata) ────────────────────────────────────────

_IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def calc_walkin_slot(now_ist: datetime) -> str:
    """Return the assigned walk-in slot label ('HH:MM AM/PM') for the given
    IST datetime. Rules (section 6):
        < 10:00  → 10:00 AM
        [10:00, 17:00) → round UP to the next 30-minute boundary
        exact 30-min boundary in range → keep it
        > 17:00 → cap at 5:00 PM
    """
    h, m = now_ist.hour, now_ist.minute
    s = now_ist.second
    if h < 10:
        slot_h, slot_m = 10, 0
    elif h >= 17:
        slot_h, slot_m = 17, 0
    else:
        # Round up to next 30-min boundary unless we're exactly on one.
        if m == 0 and s == 0:
            slot_h, slot_m = h, 0
        elif m == 30 and s == 0:
            slot_h, slot_m = h, 30
        elif m < 30:
            slot_h, slot_m = h, 30
        else:
            slot_h, slot_m = h + 1, 0
        if slot_h >= 17:
            slot_h, slot_m = 17, 0
    period = "AM" if slot_h < 12 else "PM"
    display_h = slot_h if slot_h == 12 else (slot_h % 12 if slot_h > 12 else slot_h)
    return f"{display_h:02d}:{slot_m:02d} {period}"


def _now_ist() -> datetime:
    return datetime.now(timezone.utc).astimezone(_IST)


def _slot_to_hms(label: str) -> str:
    """'10:30 AM' → '10:30:00'. Mirrors the frontend _slotToHMS helper."""
    tm, period = label.split(" ")
    h, m = [int(x) for x in tm.split(":")]
    if period == "PM" and h != 12:
        h += 12
    if period == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{m:02d}:00"


# ── Pydantic request bodies ────────────────────────────────────────────────

class ReversalRequestBody(BaseModel):
    registration_id: str
    reason_code: str  # 'wrong_candidate' | 'accidental' | 'not_present' | 'incorrect_record' | 'other'
    reason_text: Optional[str] = ""


class ReversalDecisionBody(BaseModel):
    remarks: Optional[str] = ""


class WalkInBody(BaseModel):
    registration_id: str
    physical_presence_confirmed: bool = Field(..., description="Must be true")
    chennai_availability_confirmed: bool = False
    accept_capacity_override: bool = False


# ── Audit writes ───────────────────────────────────────────────────────────

async def _write_audit(event_type: str, principal: dict, reg_id: str,
                       schedule_version: int, extra: dict) -> str:
    """Append-only event in bb_verification_events. Never updates an
    existing document. Returns the event id."""
    doc = {
        "event_type": event_type,          # VERIFIED | REVERSED | WALKIN_VERIFIED | REVERSAL_REQUESTED | REVERSAL_APPROVED | REVERSAL_REJECTED
        "registration_id": reg_id,
        "schedule_version": schedule_version,
        "actor_username": principal["username"],
        "actor_role": principal.get("role"),
        "actor_user_id": principal["user_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    r = await _db.bb_verification_events.insert_one(doc)
    return str(r.inserted_id)


# ── Feature A — Undo OTP Verification ──────────────────────────────────────

@walkin_router.post("/reversal-requests")
async def submit_reversal_request(data: ReversalRequestBody, request: Request):
    """Employee submits a correction request. Attendance stays VERIFIED
    until the admin approves. Duplicate pending requests for the same
    active verification event are rejected."""
    p = await _principal_from_request(request)
    _require(p, EMPLOYEE_REVERSAL_FLAG)

    valid_codes = {"wrong_candidate", "accidental", "not_present",
                   "incorrect_record", "other"}
    if data.reason_code not in valid_codes:
        raise HTTPException(status_code=400, detail="Invalid reason_code")
    if data.reason_code == "other" and not (data.reason_text or "").strip():
        raise HTTPException(status_code=400, detail="reason_text is required when reason_code=other")

    try:
        reg_oid = ObjectId(data.registration_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid registration_id")

    reg = await _db.bb_registrations.find_one({"_id": reg_oid})
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found")

    verified = reg.get("otp_verified")
    if verified not in (True, "1", 1):
        raise HTTPException(status_code=409, detail="This registration is not currently OTP-verified")

    schedule_version = int(reg.get("schedule_version") or 1)

    # Idempotency: no duplicate PENDING for same (registration_id, schedule_version).
    existing = await _db.bb_otp_reversal_requests.find_one({
        "registration_id": str(reg_oid),
        "schedule_version": schedule_version,
        "status": "PENDING",
    })
    if existing:
        raise HTTPException(status_code=409, detail="A pending correction request already exists for this verification")

    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "registration_id": str(reg_oid),
        "schedule_version": schedule_version,
        "reason_code": data.reason_code,
        "reason_text": (data.reason_text or "").strip(),
        "status": "PENDING",
        "requested_by_username": p["username"],
        "requested_by_user_id": p["user_id"],
        "requested_at": now,
        "reviewed_at": None,
        "reviewed_by_username": None,
        "admin_remarks": None,
    }
    r = await _db.bb_otp_reversal_requests.insert_one(doc)
    rid = str(r.inserted_id)
    await _write_audit("REVERSAL_REQUESTED", p, str(reg_oid), schedule_version,
                       {"request_id": rid, "reason_code": data.reason_code})
    return {"success": True, "request_id": rid, "status": "PENDING"}


@walkin_router.get("/reversal-requests")
async def list_reversal_requests(request: Request, status: Optional[str] = None):
    """Admins see everything. Employees see only their own requests."""
    p = await _principal_from_request(request)
    q = {}
    if status:
        q["status"] = status.upper()
    if not p["perms"].get(TOP_LEVEL_ADMIN_FLAG):
        q["requested_by_username"] = p["username"]
    rows = await _db.bb_otp_reversal_requests.find(q).sort("requested_at", -1).to_list(500)
    for r in rows:
        r["id"] = str(r.pop("_id"))
    return {"requests": rows}


@walkin_router.post("/reversal-requests/{request_id}/approve")
async def approve_reversal(request_id: str, data: ReversalDecisionBody, request: Request):
    """Admin-only. Stale requests (schedule_version drifted) are rejected
    to prevent an obsolete request from reversing a newer verification.
    On approval: atomically flip otp_verified → False, status → the
    reversed representation, otp_state → 'reversed'. Original OTP stays
    in bb_registrations.otp for same-booking reuse per Q4.7 (never
    returned, never logged, never exported)."""
    p = await _principal_from_request(request)
    _require(p, TOP_LEVEL_ADMIN_FLAG)

    try:
        rid_oid = ObjectId(request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request_id")

    req = await _db.bb_otp_reversal_requests.find_one({"_id": rid_oid})
    if not req:
        raise HTTPException(status_code=404, detail="Correction request not found")
    if req["status"] != "PENDING":
        raise HTTPException(status_code=409, detail=f"Request already {req['status']}")

    reg_oid = ObjectId(req["registration_id"])
    reg = await _db.bb_registrations.find_one({"_id": reg_oid})
    if not reg:
        raise HTTPException(status_code=404, detail="Registration vanished")

    # Staleness: current schedule_version MUST match the one captured at
    # request time. If the candidate rescheduled since, the request is obsolete.
    current_sv = int(reg.get("schedule_version") or 1)
    if current_sv != int(req["schedule_version"]):
        await _db.bb_otp_reversal_requests.update_one(
            {"_id": rid_oid},
            {"$set": {"status": "OBSOLETE",
                      "reviewed_at": datetime.now(timezone.utc).isoformat(),
                      "reviewed_by_username": p["username"],
                      "admin_remarks": "Request obsolete: schedule_version drifted"}}
        )
        raise HTTPException(status_code=409, detail="Request is obsolete; verification event no longer current")

    if reg.get("otp_verified") not in (True, "1", 1):
        raise HTTPException(status_code=409, detail="Attendance is no longer verified")

    now = datetime.now(timezone.utc).isoformat()
    # Atomic flip. `reversal_marker=True` is the signal to missed-interview
    # worker / AI dispatcher that this row is NOT a newly-missed interview.
    await _db.bb_registrations.update_one(
        {"_id": reg_oid, "otp_verified": {"$in": [True, "1", 1]},
         "schedule_version": current_sv},
        {"$set": {
            "otp_verified": False,
            "otp_state": "reversed",   # same-booking reuse gate
            "status": "Interview Scheduled",
            "reversal_marker": True,
            "reversal_marker_at": now,
            "last_update": now,
        }}
    )
    # Mirror the state in pipeline_data so Candidate Journey + exports see it.
    await _db.pipeline_data.update_many(
        {"$or": [{"phone": reg.get("phone", "")}, {"email": reg.get("email", "")}]},
        {"$set": {"otp_verified": "", "status": "Interview Scheduled",
                  "reversal_marker": True, "last_update": now}}
    )

    reversal_evt = await _write_audit(
        "REVERSAL_APPROVED", p, str(reg_oid), current_sv,
        {"request_id": request_id, "remarks": (data.remarks or "").strip()}
    )
    await _db.bb_otp_reversal_requests.update_one(
        {"_id": rid_oid},
        {"$set": {"status": "APPROVED",
                  "reviewed_at": now,
                  "reviewed_by_username": p["username"],
                  "admin_remarks": (data.remarks or "").strip(),
                  "reversal_event_id": reversal_evt}}
    )
    return {"success": True, "request_id": request_id, "status": "APPROVED",
            "reversal_event_id": reversal_evt}


@walkin_router.post("/reversal-requests/{request_id}/reject")
async def reject_reversal(request_id: str, data: ReversalDecisionBody, request: Request):
    """Admin-only. Rejection preserves VERIFIED attendance and records the
    decision + admin remarks. The employee's original reason stays intact."""
    p = await _principal_from_request(request)
    _require(p, TOP_LEVEL_ADMIN_FLAG)

    try:
        rid_oid = ObjectId(request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request_id")

    req = await _db.bb_otp_reversal_requests.find_one({"_id": rid_oid})
    if not req:
        raise HTTPException(status_code=404, detail="Correction request not found")
    if req["status"] != "PENDING":
        raise HTTPException(status_code=409, detail=f"Request already {req['status']}")

    now = datetime.now(timezone.utc).isoformat()
    await _db.bb_otp_reversal_requests.update_one(
        {"_id": rid_oid},
        {"$set": {"status": "REJECTED",
                  "reviewed_at": now,
                  "reviewed_by_username": p["username"],
                  "admin_remarks": (data.remarks or "").strip()}}
    )
    await _write_audit(
        "REVERSAL_REJECTED", p, req["registration_id"], int(req["schedule_version"]),
        {"request_id": request_id, "remarks": (data.remarks or "").strip()}
    )
    return {"success": True, "request_id": request_id, "status": "REJECTED"}


# ── Feature B — Schedule & Verify Walk-in ─────────────────────────────────

@walkin_router.post("/walk-in")
async def schedule_and_verify_walkin(data: WalkInBody, request: Request):
    """Authorized-employee-only. Atomic: eligibility check → slot calc →
    create or convert booking → generate+verify OTP internally → audit →
    suppress AI calls → suppress notifications. OTP is never returned."""
    p = await _principal_from_request(request)
    _require(p, EMPLOYEE_WALKIN_FLAG)

    if not data.physical_presence_confirmed:
        raise HTTPException(status_code=400, detail="physical_presence_confirmed must be true")

    try:
        reg_oid = ObjectId(data.registration_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid registration_id")

    reg = await _db.bb_registrations.find_one({"_id": reg_oid})
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found")

    # ─ Eligibility gates (section 5.4) ─
    if reg.get("withdrawn_by_candidate"):
        raise HTTPException(status_code=409,
                            detail="Candidate is withdrawn — complete the reactivation process first")
    if not reg.get("is_shortlisted") and reg.get("shortlisted") is not True:
        # Allow either flag name — different seed paths use different keys.
        raise HTTPException(status_code=409, detail="Candidate is not currently shortlisted")
    if reg.get("otp_verified") in (True, "1", 1):
        raise HTTPException(status_code=409,
                            detail="Candidate already has verified attendance")

    # ─ Chennai availability (section 5.5): previous NO requires explicit YES. ─
    latest_avail = reg.get("chennai_availability_latest")
    if latest_avail is False and not data.chennai_availability_confirmed:
        raise HTTPException(status_code=409,
                            detail="Candidate previously answered NO to Chennai availability; employee must confirm the new YES")

    # ─ Slot calculation (section 6) ─
    now_ist = _now_ist()
    assigned_label = calc_walkin_slot(now_ist)
    assigned_hms = _slot_to_hms(assigned_label)
    today_ist = now_ist.strftime("%Y-%m-%d")
    actual_verification_at = datetime.now(timezone.utc).isoformat()

    # ─ Capacity override (section 7) — permit but flag ─
    capacity_override = False
    cap_doc = await _db.bb_college_schedules.find_one({
        "date": today_ist, "time": assigned_hms,
    })
    if cap_doc and cap_doc.get("capacity") and cap_doc.get("booked_count", 0) >= cap_doc["capacity"]:
        capacity_override = True

    schedule_version = int(reg.get("schedule_version") or 0) + 1
    is_conversion = bool(reg.get("schedule_date"))
    prev_schedule = {
        "schedule_date": reg.get("schedule_date"),
        "schedule_time": reg.get("schedule_time"),
        "schedule_version": reg.get("schedule_version") or 1,
    } if is_conversion else None

    # ─ Chennai YES update — preserve previous NO in history ─
    chennai_updates = {}
    if latest_avail is False and data.chennai_availability_confirmed:
        prev_resp = {
            "answered_at": reg.get("chennai_availability_latest_at"),
            "latest": False,
            "reason_text": reg.get("chennai_availability_reason_text"),
            "other_text": reg.get("chennai_availability_other_text"),
            "source": "candidate_form",
        }
        chennai_updates = {
            "chennai_availability_latest": True,
            "chennai_availability_latest_at": actual_verification_at,
            "chennai_availability_source": "EMPLOYEE_VERIFIED_WALK_IN",
            "chennai_availability_history": (reg.get("chennai_availability_history") or []) + [prev_resp],
        }

    # Generate the OTP internally. Stored in `otp`; NEVER returned.
    otp = f"{secrets.randbelow(900_000) + 100_000}"

    walkin_meta = {
        "is_walk_in": True,
        "walkin_booking_source": "employee_walk_in",
        "walkin_assigned_slot_ist": f"{today_ist} {assigned_hms}",
        "walkin_actual_verification_at": actual_verification_at,
        "walkin_employee_username": p["username"],
        "walkin_employee_user_id": p["user_id"],
        "walkin_capacity_override": capacity_override,
        "walkin_hr_review_required": capacity_override,
        "walkin_previous_schedule": prev_schedule,
    }

    updates = {
        "schedule_date": today_ist,
        "schedule_time": assigned_hms,
        "schedule_version": schedule_version,
        "status": "Attended",
        "otp": otp,                                 # plaintext per existing arch (gated by _generate_otp parity)
        "otp_state": "active",
        "otp_verified": True,
        "otp_verified_source": "WALK_IN_AUTO_VERIFIED",
        "otp_verified_at": actual_verification_at,
        "otp_verified_by_username": p["username"],
        "reversal_marker": False,
        "last_update": actual_verification_at,
        # Suppress all three AI calling purposes for this schedule_version.
        "ai_suppressed_for_version": schedule_version,
        "ai_suppress_reason": "walk_in_auto_verified",
        # Suppress scheduling + OTP notifications (iter125c-era flags).
        "schedule_confirmation_suppressed": True,
        "otp_dispatch_suppressed": True,
        **walkin_meta,
        **chennai_updates,
    }

    # ─ Atomic write: single update_one with a concurrency guard matched on
    # schedule_version. If another request raced, our schedule_version check
    # will fail and we raise 409. ─
    expected_prev_sv = reg.get("schedule_version") or 0
    r = await _db.bb_registrations.update_one(
        {"_id": reg_oid, "schedule_version": expected_prev_sv},
        {"$set": updates}
    )
    if r.matched_count == 0:
        raise HTTPException(status_code=409, detail="Concurrent walk-in detected; retry after refreshing")

    # Mirror attendance state in pipeline_data so journey + exports pick up.
    await _db.pipeline_data.update_many(
        {"$or": [{"phone": reg.get("phone", "")}, {"email": reg.get("email", "")}]},
        {"$set": {
            "otp_verified": "1",
            "status": "Attended",
            "schedule_date": today_ist,
            "schedule_time": assigned_hms,
            "is_walk_in": True,
            "walkin_employee_username": p["username"],
            "walkin_capacity_override": capacity_override,
            "last_update": actual_verification_at,
        }}
    )

    # Cancel pending AI jobs for prior schedule versions (soft cancel — they
    # look up schedule_version and self-skip once mismatched).
    await _db.ai_calls_pending.update_many(
        {"registration_id": str(reg_oid), "status": "pending"},
        {"$set": {"status": "cancelled", "cancelled_reason": "walk_in_auto_verified",
                  "cancelled_at": actual_verification_at}}
    )

    walkin_evt = await _write_audit(
        "WALKIN_VERIFIED", p, str(reg_oid), schedule_version,
        {"assigned_slot": f"{today_ist} {assigned_hms}",
         "actual_verification_at": actual_verification_at,
         "capacity_override": capacity_override,
         "is_conversion": is_conversion,
         "previous_schedule": prev_schedule}
    )

    return {
        "success": True,
        "registration_id": str(reg_oid),
        "schedule_version": schedule_version,
        "assigned_interview_date_ist": today_ist,
        "assigned_interview_slot_ist": assigned_label,
        "actual_verification_at_ist": now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "is_conversion": is_conversion,
        "previous_schedule": prev_schedule,
        "capacity_override": capacity_override,
        "walkin_event_id": walkin_evt,
        # NOTE: `otp` is deliberately absent. It is never exposed anywhere.
    }
