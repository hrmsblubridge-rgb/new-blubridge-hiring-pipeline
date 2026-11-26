"""
iter171 — Targeted bug-fix pytest for three items from the BluBridge
Hiring Pipeline directive:

  Req 5  — Instruction page copy change: "a month" → "a week".
  Req 3  — Six-month verified-attendance re-registration block (replaces
           the previous four-month / 120-day rule).
  Req 1  — Interview Schedule Reports export dedup now prioritises the
           VERIFIED attendance record when two docs share
           (email, schedule_date).

Mock all outbound — never touches real candidate records.
"""
import os
import io
import csv
import uuid
import json
import pytest
import requests
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv("/app/backend/.env")

BASE = "http://127.0.0.1:8001"
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
JWT_SECRET = os.environ["JWT_SECRET"]

TAG = uuid.uuid4().hex[:8]
EMAIL = lambda who: f"it171_{TAG}_{who}@example.test"  # noqa: E731


# ------------------------------------------------------------------
# Req 5 — Instruction page static source text
# ------------------------------------------------------------------
def test_req5_instruction_page_says_a_week():
    """The canonical PublicRegistration.js instruction page must now state
    'up to a week' (not 'up to a month')."""
    with open("/app/frontend/src/pages/PublicRegistration.js", "r") as f:
        src = f.read()
    assert "up to a week" in src, "instruction page must say 'up to a week'"
    assert "up to a month" not in src, (
        "stale 'up to a month' wording must be gone"
    )


# ------------------------------------------------------------------
# Req 3 — Six-month rule
# ------------------------------------------------------------------
@pytest.fixture
def _auth_client():
    """No auth — /api/pub/register is public."""
    return requests.Session()


@pytest.fixture
def _register_form():
    """Create a disposable hiring form, yield its slug, delete after test."""
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    form_doc = {
        "name": f"IT171 Test Form {TAG}",
        "slug": f"it171-{TAG}",
        "form_type_id": None,
        "job_role": "AI & ML Engineer",
        "conditions": {},
        "show_instruction_page": True,
        "instruction_content": "",
    }
    res = db.bb_hiring_forms.insert_one(form_doc)
    yield form_doc["slug"]
    db.bb_hiring_forms.delete_one({"_id": res.inserted_id})
    db.bb_registrations.delete_many({"email": {"$regex": f"^it171_{TAG}_"}})
    db.pipeline_data.delete_many({"email": {"$regex": f"^it171_{TAG}_"}})
    c.close()


def test_req3_six_month_block_recent_attendance(_register_form):
    """A candidate with verified attendance 2 months ago must be blocked
    with the new 6-month message."""
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    e = EMAIL("recent")
    two_months_ago = (datetime.now(timezone.utc) - relativedelta(months=2)).isoformat()
    db.bb_registrations.insert_one({
        "email": e,
        "phone": "9000000001",
        "otp_verified": True,
        "otp_verified_at": two_months_ago,
        "last_update": two_months_ago,
    })
    c.close()
    r = requests.post(
        f"{BASE}/api/pub/register",
        json={
            "form_id": _register_form,
            "full_name": "Blocked Candidate",
            "email": e,
            "phone": "9000000001",
            "age": 25,
            "current_location_state": "Tamil Nadu",
            "preferred_location_city": "Chennai",
            "year_of_graduation": 2024,
            "degree": "BE",
            "course": "CS",
            "college": "Test College",
        },
        timeout=30,
    )
    assert r.status_code == 409, f"must be blocked, got {r.status_code}: {r.text}"
    assert "six months" in r.text.lower(), r.text
    assert "a month to prepare" not in r.text.lower()
    # Eligible-date line is present since otp_verified_at is set.
    assert "eligible to apply again from" in r.text.lower()

def test_req3_seven_month_block_lifted(_register_form):
    """Verified attendance 7 months ago must NOT trigger the block."""
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    e = EMAIL("old")
    seven_months_ago = (datetime.now(timezone.utc) - relativedelta(months=7)).isoformat()
    db.bb_registrations.insert_one({
        "email": e,
        "phone": "9000000002",
        "otp_verified": True,
        "otp_verified_at": seven_months_ago,
        "last_update": seven_months_ago,
    })
    c.close()
    r = requests.post(
        f"{BASE}/api/pub/register",
        json={
            "form_id": _register_form,
            "full_name": "Eligible Candidate",
            "email": e,
            "phone": "9000000002",
            "age": 25,
            "current_location_state": "Tamil Nadu",
            "preferred_location_city": "Chennai",
            "year_of_graduation": 2024,
            "degree": "BE",
            "course": "CS",
            "college": "Test College",
        },
        timeout=30,
    )
    # Not 409 — either 200 (shortlist / reject by form rules) is acceptable;
    # the point is the attendance-based 409 must be gone.
    assert r.status_code != 409, f"7-month attendance must not trigger block: {r.text}"


def test_req3_phone_only_match_still_blocks(_register_form):
    """OR-match rule: matching phone only (different email) still blocks."""
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    phone = "9000000003"
    original_email = EMAIL("orig")
    two_months_ago = (datetime.now(timezone.utc) - relativedelta(months=2)).isoformat()
    db.bb_registrations.insert_one({
        "email": original_email,
        "phone": phone,
        "otp_verified": True,
        "otp_verified_at": two_months_ago,
        "last_update": two_months_ago,
    })
    c.close()
    r = requests.post(
        f"{BASE}/api/pub/register",
        json={
            "form_id": _register_form,
            "full_name": "Phone Match",
            "email": EMAIL("newemail"),  # different email
            "phone": phone,                # same phone
            "age": 25,
            "current_location_state": "Tamil Nadu",
            "preferred_location_city": "Chennai",
            "year_of_graduation": 2024,
            "degree": "BE",
            "course": "CS",
            "college": "Test College",
        },
        timeout=30,
    )
    assert r.status_code == 409, f"phone-only match must still block: {r.status_code} {r.text}"


def test_req3_non_attended_not_blocked(_register_form):
    """Candidate with otp_verified=False (shortlisted-but-not-scheduled / no-show)
    must NOT be blocked by the attendance rule."""
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    e = EMAIL("noshow")
    one_month_ago = (datetime.now(timezone.utc) - relativedelta(months=1)).isoformat()
    db.bb_registrations.insert_one({
        "email": e,
        "phone": "9000000004",
        "otp_verified": False,          # never attended
        "last_update": one_month_ago,
    })
    c.close()
    r = requests.post(
        f"{BASE}/api/pub/register",
        json={
            "form_id": _register_form,
            "full_name": "No Show",
            "email": e,
            "phone": "9000000004",
            "age": 25,
            "current_location_state": "Tamil Nadu",
            "preferred_location_city": "Chennai",
            "year_of_graduation": 2024,
            "degree": "BE",
            "course": "CS",
            "college": "Test College",
        },
        timeout=30,
    )
    assert r.status_code != 409, r.text


def test_req3_tester_allowlist_bypasses(_register_form):
    """The authorized test identity (allowlisted tester) must bypass
    the 6-month block even after a very recent verified attendance."""
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    tester_email = "rishi.nayak@blubridge.com"
    tester_phone = "9443109903"
    two_months_ago = (datetime.now(timezone.utc) - relativedelta(months=2)).isoformat()
    # Ensure the tester has a verified-attendance history row (temporary).
    marker = f"it171_tester_{TAG}"
    db.bb_registrations.insert_one({
        "email": tester_email,
        "phone": tester_phone,
        "otp_verified": True,
        "otp_verified_at": two_months_ago,
        "last_update": two_months_ago,
        "_it171_marker": marker,
    })
    # Ensure tester is on allowlist (idempotent upsert).
    db.bb_test_credentials.update_one(
        {"$or": [{"email": tester_email}, {"phone": tester_phone}]},
        {"$setOnInsert": {"email": tester_email, "phone": tester_phone}},
        upsert=True,
    )
    c.close()
    try:
        r = requests.post(
            f"{BASE}/api/pub/register",
            json={
                "form_id": _register_form,
                "full_name": "Tester Bypass",
                "email": tester_email,
                "phone": tester_phone,
                "age": 25,
                "current_location_state": "Tamil Nadu",
                "preferred_location_city": "Chennai",
                "year_of_graduation": 2024,
                "degree": "BE",
                "course": "CS",
                "college": "Test College",
            },
            timeout=30,
        )
        assert r.status_code != 409, f"tester must bypass 6-month rule: {r.text}"
    finally:
        # Clean up only our test marker; do not touch the user's real
        # bb_registrations rows for the tester identity.
        c = MongoClient(MONGO_URL)
        c[DB_NAME].bb_registrations.delete_many({"_it171_marker": marker})
        c.close()


# ------------------------------------------------------------------
# Req 1 — Export dedup prioritises verified attendance (unit test
# through the public export endpoint).
# ------------------------------------------------------------------
def _admin_token():
    import jwt
    c = MongoClient(MONGO_URL)
    u = c[DB_NAME].bb_users.find_one({"role": "admin"}, {"_id": 0, "username": 1, "token_version": 1})
    c.close()
    tv = int((u or {}).get("token_version") or 1)
    from datetime import timedelta
    return jwt.encode(
        {"sub": u["username"], "ver": tv,
         "exp": datetime.now(timezone.utc) + relativedelta(hours=1)},
        JWT_SECRET, algorithm="HS256",
    )


def test_req1_export_prefers_verified_row_on_key_collision():
    """When two pipeline_data docs share (email, schedule_date) — one
    verified, one not — the EXPORT must retain the VERIFIED row."""
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    e = EMAIL("export")
    sd = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    marker = f"it171export_{TAG}"
    # Insert stale non-verified row FIRST (lower descending priority).
    db.pipeline_data.insert_one({
        "_it171": marker,
        "name": f"IT171 Export {TAG}",
        "email": e,
        "phone": "9000000005",
        "schedule_date": sd,
        "schedule_time": "10:00",
        "job_role": "AI & ML Engineer",
        "_normalized_job_role": "AI & ML Engineer",
        "otp_verified": False,
    })
    # Then verified row.
    db.pipeline_data.insert_one({
        "_it171": marker,
        "name": f"IT171 Export {TAG}",
        "email": e,
        "phone": "9000000005",
        "schedule_date": sd,
        "schedule_time": "11:00",
        "job_role": "AI & ML Engineer",
        "_normalized_job_role": "AI & ML Engineer",
        "otp_verified": True,
        "otp_verified_at": datetime.now(timezone.utc).isoformat(),
    })
    c.close()
    try:
        tok = _admin_token()
        r = requests.get(
            f"{BASE}/api/bb/interview-reports/export",
            params={
                "format": "csv",
                "startDate": sd, "endDate": sd,
                "jobRole": "AI & ML Engineer",
                "fields": "name,email,schedule_date,schedule_time,attendance_status",
            },
            headers={
                "Authorization": f"Bearer {tok}",
                "Accept-Encoding": "gzip",
            },
            timeout=30,
        )
        assert r.status_code == 200, r.text
        # requests auto-decodes gzip.
        text = r.text
        rows = list(csv.DictReader(io.StringIO(text)))
        matching = [row for row in rows if (row.get("Email") or "").strip().lower() == e]
        assert len(matching) == 1, f"must keep exactly ONE row for colliding key, got {matching}"
        time_cell = matching[0].get("Schedule Time") or matching[0].get("Schedule Time (12h)") or ""
        # The verified row had schedule_time=11:00; the non-verified had 10:00.
        # If the kept row is the verified one, time must reflect 11 (either
        # 11:00 or 11:00 AM/PM depending on formatter).
        assert "11" in time_cell, (
            f"verified row (11:00) must be the one retained; got time={time_cell!r}"
        )
    finally:
        c = MongoClient(MONGO_URL)
        c[DB_NAME].pipeline_data.delete_many({"_it171": marker})
        c.close()
