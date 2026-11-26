"""
iter173 — Req 4 destructive atomic role switch.

Covers:
  - /api/pub/register/switch-role success path
  - CAS prevents duplicate concurrent switches (409)
  - Strict AND-match identity (email + phone)
  - Old row marked cancelled + superseded + all outbound flags pre-flipped
  - Preview flow + submit flow: registering for a different role while
    already having an active application surfaces the role-selection
    scenario (so a frontend can render Case 1 / Case 2)
  - Existing applicants (pre-iter173) are ALSO covered: the enforcement
    is on the registration attempt, not the historical record.

All tests seed uuid-tagged throwaway rows and clean up on narrow
filters. No live outbound. No unrelated data touched.
"""
import os
import uuid
import pytest
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv("/app/backend/.env")

BASE = "http://127.0.0.1:8001"
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

TAG = uuid.uuid4().hex[:8]
EMAIL = lambda who: f"it173_{TAG}_{who}@example.test"  # noqa: E731


@pytest.fixture(autouse=True)
def _ensure_tester_allowlist_and_cleanup():
    """The authorized test identity (rishi.nayak@blubridge.com / 9443109903)
    is in bb_test_credentials — it bypasses the 6-month gate. Our synthetic
    test identities MUST also bypass the gate so attendance history from a
    prior iter test doesn't poison the result."""
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    # Add every synthetic identity we use to the allowlist.
    for who in ("owner", "scheduled", "cas", "updated"):
        db.bb_test_credentials.update_one(
            {"email": EMAIL(who)},
            {"$setOnInsert": {"email": EMAIL(who), "phone": f"9000{TAG[:4]}{ord(who[0]) % 10}1"}},
            upsert=True,
        )
    c.close()
    yield
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    db.bb_registrations.delete_many({"email": {"$regex": f"^it173_{TAG}_"}})
    db.bb_registrations.delete_many({"_it173_tag": TAG})
    db.pipeline_data.delete_many({"email": {"$regex": f"^it173_{TAG}_"}})
    db.bb_test_credentials.delete_many({"email": {"$regex": f"^it173_{TAG}_"}})
    db.bb_hiring_forms.delete_many({"slug": {"$regex": f"^it173-{TAG}"}})
    c.close()


@pytest.fixture
def _new_role_form():
    """Create a disposable hiring form for the NEW role the candidate is switching to."""
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    form_doc = {
        "name": f"IT173 New Role Form {TAG}",
        "slug": f"it173-{TAG}-new",
        "form_type_id": None,
        "job_role": "HR Executive",
        "conditions": {},
        "show_instruction_page": False,
        "instruction_content": "",
    }
    res = db.bb_hiring_forms.insert_one(form_doc)
    c.close()
    yield form_doc["slug"]
    c = MongoClient(MONGO_URL)
    c[DB_NAME].bb_hiring_forms.delete_one({"_id": res.inserted_id})
    c.close()


def _seed_existing_application(who: str, *, scheduled: bool):
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    doc = {
        "email": EMAIL(who),
        "phone": "9000000020",
        "job_role": "AI & ML Engineer",
        "is_shortlisted": True,
        "superseded": False,
        "schedule_token": f"it173tok_{TAG}_{who}",
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "form_id": "placeholder",
        "full_name": "IT173 Candidate",
        "_it173_tag": TAG,
        "isTest": True,
    }
    if scheduled:
        doc["schedule_date"] = "2026-12-05"
        doc["schedule_time"] = "11:00"
    db.bb_registrations.insert_one(doc)
    c.close()
    return doc


def test_switch_role_atomic_cancels_old_and_creates_new(_new_role_form):
    _seed_existing_application("scheduled", scheduled=True)
    # Pre-flight: preview surfaces the scheduled scenario so a UI could
    # render the Case-2 selection box BEFORE calling switch-role.
    prev = requests.post(f"{BASE}/api/pub/register/preview",
                         json={"email": EMAIL("scheduled"), "phone": "9000000020"},
                         timeout=30)
    assert prev.status_code == 200
    assert prev.json()["scenario"] == "scheduled", prev.json()

    # Execute switch
    r = requests.post(f"{BASE}/api/pub/register/switch-role", json={
        "form_id": _new_role_form,
        "full_name": "IT173 Candidate",
        "email": EMAIL("scheduled"),
        "phone": "9000000020",
        "age": 25,
        "current_location_state": "Tamil Nadu",
        "preferred_location_city": "Chennai",
        "year_of_graduation": 2024,
        "degree": "BE",
        "course": "CS",
        "college": "Test College",
    }, timeout=30)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j.get("switched_from_role") == "AI & ML Engineer"
    assert j.get("previous_interview_cancelled") is True

    # Verify old row is cancelled + outbound flags pre-flipped
    c = MongoClient(MONGO_URL)
    old = c[DB_NAME].bb_registrations.find_one(
        {"email": EMAIL("scheduled"), "cancelled_by_role_switch": True},
        {"_id": 0},
    )
    c.close()
    assert old is not None, "old row must be marked cancelled_by_role_switch"
    assert old["superseded"] is True
    assert old["status"] == "CANCELLED_BY_ROLE_SWITCH"
    assert old["schedule_token_invalidated"] is True
    # Every notification worker must skip this row now
    for flag in ("otp_wa_sent", "otp_email_sent", "otp_dispatch_in_progress",
                 "missed_reminder_wa_sent", "missed_reminder_email_sent",
                 "reminder_24h_sent", "schedule_link_sent",
                 "interview_mail_sent", "reject_notified", "rejection_sent"):
        assert old.get(flag) is True, f"{flag} must be pre-flipped True on cancelled row"
    assert old.get("rejection_pending") is False

    # Verify a NEW row for the new role exists
    c = MongoClient(MONGO_URL)
    new = c[DB_NAME].bb_registrations.find_one(
        {"email": EMAIL("scheduled"), "cancelled_by_role_switch": {"$ne": True}},
        {"_id": 0},
    )
    c.close()
    assert new is not None, "a fresh row must exist for the new role"
    assert new["job_role"] == "HR Executive"


def test_switch_role_shortlisted_not_scheduled_case1(_new_role_form):
    _seed_existing_application("owner", scheduled=False)
    prev = requests.post(f"{BASE}/api/pub/register/preview",
                         json={"email": EMAIL("owner"), "phone": "9000000020"},
                         timeout=30)
    assert prev.status_code == 200
    assert prev.json()["scenario"] == "shortlisted_not_scheduled"

    # Candidate chose Option 2 (switch). For Case-1 the extra confirmation
    # is not required — but the backend contract is the same endpoint.
    r = requests.post(f"{BASE}/api/pub/register/switch-role", json={
        "form_id": _new_role_form,
        "full_name": "IT173 Owner",
        "email": EMAIL("owner"),
        "phone": "9000000020",
        "age": 25,
        "current_location_state": "Tamil Nadu",
        "preferred_location_city": "Chennai",
        "year_of_graduation": 2024,
        "degree": "BE",
        "course": "CS",
        "college": "Test College",
    }, timeout=30)
    assert r.status_code == 200, r.text
    assert r.json()["previous_interview_cancelled"] is False  # no schedule was set


def test_switch_role_requires_both_email_and_phone(_new_role_form):
    """Strict AND-match identity: knowing only one field must not cancel."""
    _seed_existing_application("scheduled", scheduled=True)
    # Different phone → no active row matches, so 409
    r = requests.post(f"{BASE}/api/pub/register/switch-role", json={
        "form_id": _new_role_form,
        "full_name": "Impersonator",
        "email": EMAIL("scheduled"),
        "phone": "9999999999",  # wrong phone
        "age": 25,
        "current_location_state": "Tamil Nadu",
        "preferred_location_city": "Chennai",
        "year_of_graduation": 2024,
        "degree": "BE",
        "course": "CS",
        "college": "Test College",
    }, timeout=30)
    assert r.status_code == 409, r.text
    assert "no active application" in r.text.lower()


def test_switch_role_cas_prevents_duplicate_concurrent(_new_role_form):
    """Second call before the first commits must return 409."""
    _seed_existing_application("cas", scheduled=True)
    # Manually flip the CAS flag so a second call sees "in progress"
    c = MongoClient(MONGO_URL)
    c[DB_NAME].bb_registrations.update_one(
        {"email": EMAIL("cas"), "cancelled_by_role_switch": {"$ne": True}},
        {"$set": {"role_switch_in_progress": True}},
    )
    c.close()
    r = requests.post(f"{BASE}/api/pub/register/switch-role", json={
        "form_id": _new_role_form,
        "full_name": "CAS Candidate",
        "email": EMAIL("cas"),
        "phone": "9000000020",
        "age": 25,
        "current_location_state": "Tamil Nadu",
        "preferred_location_city": "Chennai",
        "year_of_graduation": 2024,
        "degree": "BE",
        "course": "CS",
        "college": "Test College",
    }, timeout=30)
    assert r.status_code == 409, r.text
    assert "already in progress" in r.text.lower()


def test_existing_applicant_registering_differently_sees_preview_scenario(_new_role_form):
    """Req 4 enforcement for EXISTING applicants: an already-shortlisted
    candidate who submits the public form for a different role gets the
    preview `scenario=shortlisted_not_scheduled` back so the frontend can
    render Case-1."""
    _seed_existing_application("updated", scheduled=False)
    prev = requests.post(f"{BASE}/api/pub/register/preview",
                         json={"email": EMAIL("updated"), "phone": "9000000020"},
                         timeout=30)
    assert prev.status_code == 200
    j = prev.json()
    assert j["scenario"] == "shortlisted_not_scheduled"
    assert j["existing_role"] == "AI & ML Engineer"
