"""
iter175 — Tester identities bypass EVERY existing-application scenario.

For any identity whose email OR phone is present in `bb_test_credentials`:
  - /preview returns `{"scenario": "none"}` unconditionally (no 6-month
    block, no same-role screen, no Case-1 / Case-2 role selection).
  - /register proceeds normally — the existing supersede mechanism in
    `register_applicant` ensures only the latest submission remains the
    active row.
  - Every submission sends the usual shortlist / rejection notifications,
    as if it were the tester's first time (gated by the existing
    messaging CAS + superseded filter, which naturally resets on a new
    `registered_at`).

Cleans up only the tagged throwaway rows it creates.
"""
import os
import uuid
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

TAG = uuid.uuid4().hex[:8]
TESTER_EMAIL = f"it175_tester_{TAG}@example.test"
TESTER_PHONE = f"9000{TAG[:6]}"[:10].ljust(10, "0")


@pytest.fixture(autouse=True)
def _tester_fixture():
    """Promote a disposable identity to tester for the duration of this test."""
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    db.bb_test_credentials.update_one(
        {"email": TESTER_EMAIL},
        {"$set": {"email": TESTER_EMAIL, "phone": TESTER_PHONE}},
        upsert=True,
    )
    c.close()
    yield
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    db.bb_test_credentials.delete_many({"email": TESTER_EMAIL})
    db.bb_registrations.delete_many({"email": TESTER_EMAIL})
    db.bb_registrations.delete_many({"_it175_tag": TAG})
    c.close()


def _preview(email=None, phone=None, new_role=""):
    return requests.post(
        f"{BASE}/api/pub/register/preview",
        json={
            "email": email or TESTER_EMAIL,
            "phone": phone or TESTER_PHONE,
            "new_role": new_role,
        },
        timeout=30,
    )


def _seed(**overrides):
    """Seed an existing active application for the tester."""
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    doc = {
        "email": TESTER_EMAIL,
        "phone": TESTER_PHONE,
        "job_role": "AI & ML Engineer",
        "is_shortlisted": True,
        "superseded": False,
        "schedule_token": f"it175tok_{TAG}",
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "_it175_tag": TAG,
    }
    doc.update(overrides)
    db.bb_registrations.insert_one(doc)
    c.close()


def test_tester_bypass_with_scheduled_interview_still_returns_none():
    """Even when the tester has an existing SCHEDULED interview, preview
    must return scenario=none so the register flow proceeds fresh."""
    _seed(schedule_date="2026-12-05", schedule_time="11:00")
    r = _preview(new_role="AI & ML Engineer")   # SAME role
    assert r.status_code == 200
    assert r.json() == {"scenario": "none"}, r.json()


def test_tester_bypass_with_shortlisted_not_scheduled_still_returns_none():
    _seed()                                     # shortlisted, no schedule
    r = _preview(new_role="AI & ML Engineer")
    assert r.json() == {"scenario": "none"}


def test_tester_bypass_different_role_still_returns_none():
    _seed()
    r = _preview(new_role="HR Executive")       # different role
    assert r.json() == {"scenario": "none"}


def test_tester_bypass_with_verified_attendance_within_six_months():
    """A tester with otp_verified=True from 2 months ago must still get
    scenario=none — the 6-month rule does NOT apply to testers."""
    two_months_ago = (datetime.now(timezone.utc) - relativedelta(months=2)).isoformat()
    _seed(
        otp_verified=True,
        otp_verified_at=two_months_ago,
        last_update=two_months_ago,
    )
    r = _preview(new_role="AI & ML Engineer")
    assert r.json() == {"scenario": "none"}


def test_tester_phone_only_match_still_bypasses():
    """OR-match: a submission with the tester's phone but a different
    email must still bypass (same rule messaging.can_send_message uses)."""
    _seed()
    r = _preview(email="totally.different@example.test", phone=TESTER_PHONE,
                 new_role="AI & ML Engineer")
    assert r.json() == {"scenario": "none"}


def test_non_tester_identity_still_sees_existing_application():
    """Sanity: non-tester with same stored data still sees the scenario."""
    _seed(email=f"it175_nontester_{TAG}@example.test",
          phone="9876543210")
    try:
        r = requests.post(f"{BASE}/api/pub/register/preview", json={
            "email": f"it175_nontester_{TAG}@example.test",
            "phone": "9876543210",
            "new_role": "AI & ML Engineer",
        }, timeout=30)
        assert r.json()["scenario"] == "same_role_shortlisted_not_scheduled", r.json()
    finally:
        c = MongoClient(MONGO_URL)
        c[DB_NAME].bb_registrations.delete_many({"email": f"it175_nontester_{TAG}@example.test"})
        c.close()
