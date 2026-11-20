"""iter163 — Manual OTP Verification Enhancement A + B — regression suite.

Covers:
* Feature A — Undo OTP Verification (submit / admin approve / reject / staleness / audit)
* Feature B — Schedule & Verify Walk-in (slot calc, eligibility, conversion, capacity,
  AI suppression, OTP reuse after reversal, OTP-never-exposed)

SAFETY: scratch DBs, mocked principals. Backend `bb_walkin.init_walkin`
refuses to bind to hr_analytics unless WALKIN_ALLOW_PROD=1.
"""
import asyncio
import io
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI, Request
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import AutoReconnect

import bb_walkin

SCRATCH = "ts_iter163_pytest_scratch"
IST = timezone(timedelta(hours=5, minutes=30), name="IST")


# ─── Slot calculation — unit tests (pure, no DB) ──────────────────────────

@pytest.mark.parametrize("hhmm, expected", [
    ((9, 15), "10:00 AM"),
    ((10, 0), "10:00 AM"),
    ((10, 10), "10:30 AM"),
    ((14, 15), "02:30 PM"),
    ((14, 18), "02:30 PM"),
    ((14, 30), "02:30 PM"),
    ((15, 40), "04:00 PM"),
    ((16, 45), "05:00 PM"),
    ((17, 10), "05:00 PM"),
    ((18, 30), "05:00 PM"),
    ((11, 0), "11:00 AM"),
    ((11, 30), "11:30 AM"),
    ((16, 59), "05:00 PM"),
])
def test_calc_walkin_slot(hhmm, expected):
    h, m = hhmm
    dt = datetime(2026, 2, 4, h, m, 0, tzinfo=IST)
    assert bb_walkin.calc_walkin_slot(dt) == expected


# ─── Integration harness ──────────────────────────────────────────────────

def _seed_user(role="admin", perms=None):
    return {
        "username": f"user-{role}-{uuid.uuid4().hex[:6]}",
        "role": role,
        "permissions": perms or {},
    }


def run(case, *, admin_perms=None, employee_perms=None):
    async def main():
        client = AsyncIOMotorClient("mongodb://localhost:27017")
        dbn = f"{SCRATCH}_{uuid.uuid4().hex[:8]}"
        d = client[dbn]

        admin = _seed_user("admin", admin_perms or {
            "can_approve_otp_reversal": True,
            "can_request_otp_reversal": True,
            "can_schedule_walkin": True,
        })
        employee = _seed_user("employee", employee_perms or {
            "can_approve_otp_reversal": False,
            "can_request_otp_reversal": True,
            "can_schedule_walkin": True,
        })
        unprivileged = _seed_user("employee", {})

        await d.bb_users.insert_many([admin.copy(), employee.copy(), unprivileged.copy()])

        # Pluggable principal: switch the caller via header.
        async def auth(request: Request):
            u = request.headers.get("X-Test-User") or admin["username"]
            return u

        prev_db, prev_auth = bb_walkin._db, bb_walkin._auth_fn
        bb_walkin._db, bb_walkin._auth_fn = d, auth
        try:
            app = FastAPI()
            app.include_router(bb_walkin.walkin_router)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://t",
            ) as tc:
                await case(tc, d, {"admin": admin, "employee": employee, "unprivileged": unprivileged})
        finally:
            client.close()
            bb_walkin._db, bb_walkin._auth_fn = prev_db, prev_auth

    for attempt in range(3):
        try:
            asyncio.run(main())
            break
        except AutoReconnect:
            if attempt == 2:
                raise


def _hdr(who):
    return {"X-Test-User": who["username"]}


async def _mk_reg(d, *, verified=True, shortlisted=True, withdrawn=False,
                  chennai=True, schedule_date="2026-03-10"):
    reg = {
        "email": f"c-{uuid.uuid4().hex[:6]}@t.com",
        "phone": "9" + "".join([str((i * 7) % 10) for i in range(9)]),
        "full_name": f"Cand-{uuid.uuid4().hex[:4]}",
        "job_role": "SDE",
        "schedule_date": schedule_date,
        "schedule_time": "10:00:00",
        "schedule_version": 1,
        "otp": "123456",
        "otp_verified": verified,
        "status": "Attended" if verified else "Interview Scheduled",
        "is_shortlisted": shortlisted,
        "withdrawn_by_candidate": withdrawn,
        "chennai_availability_latest": chennai,
    }
    r = await d.bb_registrations.insert_one(reg)
    reg["_id"] = r.inserted_id
    return reg


# ─── Feature A — Reversal request lifecycle ───────────────────────────────

def test_employee_submits_request_and_attendance_stays_verified():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=True)
        r = await tc.post(
            "/api/bb/manual-otp/reversal-requests",
            json={"registration_id": str(reg["_id"]),
                  "reason_code": "wrong_candidate", "reason_text": ""},
            headers=_hdr(u["employee"]),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "PENDING"
        # Attendance unchanged.
        reg2 = await d.bb_registrations.find_one({"_id": reg["_id"]})
        assert reg2["otp_verified"] is True
    run(case)


def test_unprivileged_employee_blocked_from_submitting():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=True)
        r = await tc.post(
            "/api/bb/manual-otp/reversal-requests",
            json={"registration_id": str(reg["_id"]),
                  "reason_code": "accidental"},
            headers=_hdr(u["unprivileged"]),
        )
        assert r.status_code == 403
    run(case)


def test_other_reason_requires_text():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=True)
        r = await tc.post(
            "/api/bb/manual-otp/reversal-requests",
            json={"registration_id": str(reg["_id"]), "reason_code": "other"},
            headers=_hdr(u["employee"]),
        )
        assert r.status_code == 400
    run(case)


def test_duplicate_pending_request_blocked():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=True)
        body = {"registration_id": str(reg["_id"]), "reason_code": "accidental"}
        r1 = await tc.post("/api/bb/manual-otp/reversal-requests", json=body, headers=_hdr(u["employee"]))
        r2 = await tc.post("/api/bb/manual-otp/reversal-requests", json=body, headers=_hdr(u["employee"]))
        assert r1.status_code == 200
        assert r2.status_code == 409
    run(case)


def test_employee_cannot_approve_own_request():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=True)
        s = await tc.post("/api/bb/manual-otp/reversal-requests",
                          json={"registration_id": str(reg["_id"]), "reason_code": "accidental"},
                          headers=_hdr(u["employee"]))
        rid = s.json()["request_id"]
        # Employee lacks can_approve_otp_reversal.
        r = await tc.post(f"/api/bb/manual-otp/reversal-requests/{rid}/approve",
                          json={"remarks": ""}, headers=_hdr(u["employee"]))
        assert r.status_code == 403
    run(case)


def test_admin_approves_and_attendance_flips_to_not_verified():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=True)
        s = await tc.post("/api/bb/manual-otp/reversal-requests",
                          json={"registration_id": str(reg["_id"]), "reason_code": "accidental"},
                          headers=_hdr(u["employee"]))
        rid = s.json()["request_id"]
        r = await tc.post(f"/api/bb/manual-otp/reversal-requests/{rid}/approve",
                          json={"remarks": "approved by top-level admin"},
                          headers=_hdr(u["admin"]))
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "APPROVED"
        reg2 = await d.bb_registrations.find_one({"_id": reg["_id"]})
        assert reg2["otp_verified"] is False
        assert reg2["otp_state"] == "reversed"
        assert reg2["reversal_marker"] is True
        # Original OTP preserved for same-booking reuse.
        assert reg2["otp"] == "123456"
        # Audit chain: REVERSAL_REQUESTED + REVERSAL_APPROVED.
        kinds = [e["event_type"] async for e in d.bb_verification_events.find({"registration_id": str(reg["_id"])})]
        assert "REVERSAL_REQUESTED" in kinds
        assert "REVERSAL_APPROVED" in kinds
    run(case)


def test_admin_rejects_preserves_verified():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=True)
        s = await tc.post("/api/bb/manual-otp/reversal-requests",
                          json={"registration_id": str(reg["_id"]), "reason_code": "not_present"},
                          headers=_hdr(u["employee"]))
        rid = s.json()["request_id"]
        r = await tc.post(f"/api/bb/manual-otp/reversal-requests/{rid}/reject",
                          json={"remarks": "candidate confirmed present"},
                          headers=_hdr(u["admin"]))
        assert r.status_code == 200
        reg2 = await d.bb_registrations.find_one({"_id": reg["_id"]})
        assert reg2["otp_verified"] is True
    run(case)


def test_stale_request_cannot_reverse_newer_verification():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=True)
        s = await tc.post("/api/bb/manual-otp/reversal-requests",
                          json={"registration_id": str(reg["_id"]), "reason_code": "accidental"},
                          headers=_hdr(u["employee"]))
        rid = s.json()["request_id"]
        # Candidate rescheduled → schedule_version bumped.
        await d.bb_registrations.update_one(
            {"_id": reg["_id"]},
            {"$set": {"schedule_version": 2}}
        )
        r = await tc.post(f"/api/bb/manual-otp/reversal-requests/{rid}/approve",
                          json={"remarks": ""}, headers=_hdr(u["admin"]))
        assert r.status_code == 409
        req = await d.bb_otp_reversal_requests.find_one({"_id": ObjectId(rid)})
        assert req["status"] == "OBSOLETE"
    run(case)


# ─── Feature B — Walk-in ──────────────────────────────────────────────────

def test_walkin_requires_physical_presence():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=False, schedule_date=None)
        r = await tc.post("/api/bb/manual-otp/walk-in",
                          json={"registration_id": str(reg["_id"]),
                                "physical_presence_confirmed": False},
                          headers=_hdr(u["employee"]))
        assert r.status_code == 400
    run(case)


def test_walkin_unauthorized_employee_blocked():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=False, schedule_date=None)
        r = await tc.post("/api/bb/manual-otp/walk-in",
                          json={"registration_id": str(reg["_id"]),
                                "physical_presence_confirmed": True},
                          headers=_hdr(u["unprivileged"]))
        assert r.status_code == 403
    run(case)


def test_walkin_blocks_withdrawn():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=False, withdrawn=True, schedule_date=None)
        r = await tc.post("/api/bb/manual-otp/walk-in",
                          json={"registration_id": str(reg["_id"]),
                                "physical_presence_confirmed": True},
                          headers=_hdr(u["employee"]))
        assert r.status_code == 409
    run(case)


def test_walkin_blocks_unshortlisted():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=False, shortlisted=False, schedule_date=None)
        r = await tc.post("/api/bb/manual-otp/walk-in",
                          json={"registration_id": str(reg["_id"]),
                                "physical_presence_confirmed": True},
                          headers=_hdr(u["employee"]))
        assert r.status_code == 409
    run(case)


def test_walkin_blocks_already_verified():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=True)
        r = await tc.post("/api/bb/manual-otp/walk-in",
                          json={"registration_id": str(reg["_id"]),
                                "physical_presence_confirmed": True},
                          headers=_hdr(u["employee"]))
        assert r.status_code == 409
    run(case)


def test_walkin_chennai_no_requires_explicit_yes_confirmation():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=False, chennai=False, schedule_date=None)
        # Without chennai_availability_confirmed → blocked
        r1 = await tc.post("/api/bb/manual-otp/walk-in",
                           json={"registration_id": str(reg["_id"]),
                                 "physical_presence_confirmed": True,
                                 "chennai_availability_confirmed": False},
                           headers=_hdr(u["employee"]))
        assert r1.status_code == 409
        # With confirmation → proceeds
        r2 = await tc.post("/api/bb/manual-otp/walk-in",
                           json={"registration_id": str(reg["_id"]),
                                 "physical_presence_confirmed": True,
                                 "chennai_availability_confirmed": True},
                           headers=_hdr(u["employee"]))
        assert r2.status_code == 200
        # Previous NO preserved in history.
        reg2 = await d.bb_registrations.find_one({"_id": reg["_id"]})
        hist = reg2.get("chennai_availability_history") or []
        assert any(h["latest"] is False for h in hist)
        assert reg2["chennai_availability_latest"] is True
        assert reg2["chennai_availability_source"] == "EMPLOYEE_VERIFIED_WALK_IN"
    run(case)


def test_walkin_happy_path_atomic_verification_and_no_otp_leak():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=False, schedule_date=None)
        r = await tc.post("/api/bb/manual-otp/walk-in",
                          json={"registration_id": str(reg["_id"]),
                                "physical_presence_confirmed": True},
                          headers=_hdr(u["employee"]))
        assert r.status_code == 200, r.text
        body = r.json()
        # OTP never in response.
        assert "otp" not in body
        for k, v in body.items():
            if isinstance(v, str):
                assert "123456" not in v  # seeded OTP string for cross-check
        reg2 = await d.bb_registrations.find_one({"_id": reg["_id"]})
        assert reg2["otp_verified"] is True
        assert reg2["otp_verified_source"] == "WALK_IN_AUTO_VERIFIED"
        assert reg2["status"] == "Attended"
        assert reg2["schedule_version"] == 2  # bumped from 1
        assert reg2["is_walk_in"] is True
        assert reg2["ai_suppress_reason"] == "walk_in_auto_verified"
        # Audit event recorded.
        events = [e async for e in d.bb_verification_events.find({"registration_id": str(reg["_id"])})]
        assert any(e["event_type"] == "WALKIN_VERIFIED" for e in events)
    run(case)


def test_walkin_converts_existing_future_booking():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=False, schedule_date="2026-05-10")
        r = await tc.post("/api/bb/manual-otp/walk-in",
                          json={"registration_id": str(reg["_id"]),
                                "physical_presence_confirmed": True},
                          headers=_hdr(u["employee"]))
        assert r.status_code == 200
        assert r.json()["is_conversion"] is True
        prev = r.json()["previous_schedule"]
        assert prev["schedule_date"] == "2026-05-10"
        reg2 = await d.bb_registrations.find_one({"_id": reg["_id"]})
        # No duplicate booking — same reg doc, schedule_date is today's IST.
        assert reg2["schedule_version"] == 2
        assert reg2["walkin_previous_schedule"]["schedule_date"] == "2026-05-10"
    run(case)


def test_walkin_cancels_pending_ai_calls():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=False, schedule_date=None)
        # Seed a pending AI job (would otherwise fire).
        await d.ai_calls_pending.insert_one({
            "registration_id": str(reg["_id"]),
            "purpose": "booking_confirmation",
            "status": "pending",
        })
        r = await tc.post("/api/bb/manual-otp/walk-in",
                          json={"registration_id": str(reg["_id"]),
                                "physical_presence_confirmed": True},
                          headers=_hdr(u["employee"]))
        assert r.status_code == 200
        job = await d.ai_calls_pending.find_one({"registration_id": str(reg["_id"])})
        assert job["status"] == "cancelled"
        assert job["cancelled_reason"] == "walk_in_auto_verified"
    run(case)


def test_walkin_capacity_override_flag():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=False, schedule_date=None)
        # Seed capacity cap for TODAY at the slot the engine will assign.
        now_ist = bb_walkin._now_ist()
        slot = bb_walkin.calc_walkin_slot(now_ist)
        slot_hms = bb_walkin._slot_to_hms(slot)
        today = now_ist.strftime("%Y-%m-%d")
        await d.bb_college_schedules.insert_one({
            "date": today, "time": slot_hms,
            "capacity": 1, "booked_count": 5,
        })
        r = await tc.post("/api/bb/manual-otp/walk-in",
                          json={"registration_id": str(reg["_id"]),
                                "physical_presence_confirmed": True,
                                "accept_capacity_override": True},
                          headers=_hdr(u["employee"]))
        assert r.status_code == 200
        assert r.json()["capacity_override"] is True
        reg2 = await d.bb_registrations.find_one({"_id": reg["_id"]})
        assert reg2["walkin_hr_review_required"] is True
    run(case)


# ─── Feature A + B — OTP reuse after reversal ────────────────────────────

def test_approved_reversal_permits_same_otp_reuse_same_booking():
    """Per Q4.7: after admin-approved reversal, the ORIGINAL OTP on the
    same registration + same schedule_version may be used again. The OTP
    stays in `bb_registrations.otp` (plaintext, pre-existing) and is
    never returned by any endpoint we added."""
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=True)
        s = await tc.post("/api/bb/manual-otp/reversal-requests",
                          json={"registration_id": str(reg["_id"]), "reason_code": "accidental"},
                          headers=_hdr(u["employee"]))
        rid = s.json()["request_id"]
        await tc.post(f"/api/bb/manual-otp/reversal-requests/{rid}/approve",
                      json={"remarks": ""}, headers=_hdr(u["admin"]))
        reg2 = await d.bb_registrations.find_one({"_id": reg["_id"]})
        # OTP intact + reusable: otp_state=reversed, otp_verified=False, same otp.
        assert reg2["otp"] == "123456"
        assert reg2["otp_state"] == "reversed"
        assert reg2["otp_verified"] is False
        assert reg2["schedule_version"] == 1  # same booking
    run(case)


# ─── iter163b — Admin direct-revoke ───────────────────────────────────────

def test_me_endpoint_returns_effective_perms():
    async def case(tc, d, u):
        r = await tc.get("/api/bb/manual-otp/me", headers=_hdr(u["admin"]))
        assert r.status_code == 200
        body = r.json()
        assert body["username"] == u["admin"]["username"]
        assert body["permissions"]["can_approve_otp_reversal"] is True
        r2 = await tc.get("/api/bb/manual-otp/me", headers=_hdr(u["employee"]))
        body2 = r2.json()
        assert body2["permissions"]["can_approve_otp_reversal"] is False
    run(case)


def test_admin_direct_revoke_flips_attendance():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=True)
        r = await tc.post("/api/bb/manual-otp/admin-revoke",
                          json={"registration_id": str(reg["_id"]),
                                "remarks": "verified in error"},
                          headers=_hdr(u["admin"]))
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "APPROVED"
        assert body["source"] == "admin_direct"
        reg2 = await d.bb_registrations.find_one({"_id": reg["_id"]})
        assert reg2["otp_verified"] is False
        assert reg2["otp_state"] == "reversed"
        assert reg2["reversal_marker"] is True
        # Original OTP preserved for same-booking reuse.
        assert reg2["otp"] == "123456"
        # Correction request record present and marked admin_direct.
        req = await d.bb_otp_reversal_requests.find_one({"registration_id": str(reg["_id"])})
        assert req["request_source"] == "admin_direct"
        assert req["status"] == "APPROVED"
        # Audit event logged.
        kinds = [e["event_type"] async for e in d.bb_verification_events.find({"registration_id": str(reg["_id"])})]
        assert "REVERSAL_APPROVED" in kinds
    run(case)


def test_admin_direct_revoke_unauthorized_employee_blocked():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=True)
        r = await tc.post("/api/bb/manual-otp/admin-revoke",
                          json={"registration_id": str(reg["_id"])},
                          headers=_hdr(u["employee"]))
        assert r.status_code == 403
        reg2 = await d.bb_registrations.find_one({"_id": reg["_id"]})
        assert reg2["otp_verified"] is True  # unchanged
    run(case)


def test_admin_direct_revoke_requires_currently_verified():
    async def case(tc, d, u):
        reg = await _mk_reg(d, verified=False)  # not yet verified
        r = await tc.post("/api/bb/manual-otp/admin-revoke",
                          json={"registration_id": str(reg["_id"])},
                          headers=_hdr(u["admin"]))
        assert r.status_code == 409
    run(case)
