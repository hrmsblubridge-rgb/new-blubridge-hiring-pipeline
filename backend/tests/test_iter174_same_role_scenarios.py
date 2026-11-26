"""
iter174 — Same-role duplicate registration scenarios.

Covers:
  - Scenario A: Candidate shortlisted for Role X, not yet scheduled, applies
    AGAIN for Role X → scenario="same_role_shortlisted_not_scheduled" with
    schedule_token in the response so the frontend can show a direct
    "Schedule Interview" button.
  - Scenario B: Candidate shortlisted for Role X, HAS scheduled, applies
    AGAIN for Role X → scenario="same_role_scheduled" with date/time in
    the response.

STRICT: No live outbound. No live-data mutation. uuid-tagged seeds only.
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
EMAIL = lambda who: f"it174_{TAG}_{who}@example.test"  # noqa: E731


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    db.bb_registrations.delete_many({"email": {"$regex": f"^it174_{TAG}_"}})
    db.bb_registrations.delete_many({"_it174_tag": TAG})
    c.close()


def _seed(who: str, *, scheduled: bool, role: str = "AI & ML Engineer"):
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    doc = {
        "email": EMAIL(who),
        "phone": "9000000030",
        "job_role": role,
        "is_shortlisted": True,
        "superseded": False,
        "schedule_token": f"it174tok_{TAG}_{who}",
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "_it174_tag": TAG,
    }
    if scheduled:
        doc["schedule_date"] = "2026-12-10"
        doc["schedule_time"] = "14:30"
    db.bb_registrations.insert_one(doc)
    c.close()


def test_same_role_shortlisted_not_scheduled_returns_token():
    _seed("sl", scheduled=False)
    r = requests.post(f"{BASE}/api/pub/register/preview", json={
        "email": EMAIL("sl"), "phone": "9000000030",
        "new_role": "AI & ML Engineer",
    }, timeout=30)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["scenario"] == "same_role_shortlisted_not_scheduled", j
    assert j["existing_role"] == "AI & ML Engineer"
    assert j["schedule_token"] == f"it174tok_{TAG}_sl"


def test_same_role_scheduled_returns_date_time():
    _seed("sch", scheduled=True)
    r = requests.post(f"{BASE}/api/pub/register/preview", json={
        "email": EMAIL("sch"), "phone": "9000000030",
        "new_role": "AI & ML Engineer",
    }, timeout=30)
    assert r.status_code == 200
    j = r.json()
    assert j["scenario"] == "same_role_scheduled"
    assert j["existing_role"] == "AI & ML Engineer"
    assert j["schedule_date"] == "2026-12-10"
    assert j["schedule_time"] == "14:30"
    # Token present but UI doesn't need to use it in Scenario B.
    assert j["schedule_token"] == f"it174tok_{TAG}_sch"


def test_same_role_case_insensitive_match():
    _seed("ci", scheduled=False)
    r = requests.post(f"{BASE}/api/pub/register/preview", json={
        "email": EMAIL("ci"), "phone": "9000000030",
        "new_role": "ai & ml engineer",   # lowercase vs stored "AI & ML Engineer"
    }, timeout=30)
    assert r.json()["scenario"] == "same_role_shortlisted_not_scheduled"


def test_different_role_falls_through_to_role_select():
    _seed("diff", scheduled=False)
    r = requests.post(f"{BASE}/api/pub/register/preview", json={
        "email": EMAIL("diff"), "phone": "9000000030",
        "new_role": "HR Executive",   # different role
    }, timeout=30)
    assert r.json()["scenario"] == "shortlisted_not_scheduled"


def test_same_role_token_requires_and_match_identity():
    """OR-match (only one of email/phone) must NOT expose the schedule_token.
    This prevents a third party who knows only the email OR only the phone
    from grabbing a scheduling link."""
    _seed("andmatch", scheduled=False)
    # Wrong phone, right email — OR-match still finds row, but should NOT
    # expose same_role scenario because both fields didn't verify ownership.
    r = requests.post(f"{BASE}/api/pub/register/preview", json={
        "email": EMAIL("andmatch"),
        "phone": "9999999999",  # does not match stored row
        "new_role": "AI & ML Engineer",
    }, timeout=30)
    j = r.json()
    assert j["scenario"] == "shortlisted_not_scheduled", (
        f"OR-match on a different role→same-name but with phone mismatch must "
        f"NOT leak schedule_token; got {j}"
    )
    assert "schedule_token" not in j


def test_preview_without_new_role_uses_legacy_scenarios():
    """Omitting `new_role` must preserve the original iter172 contract so
    other callers (admin dashboards) are unaffected."""
    _seed("legacy", scheduled=False)
    r = requests.post(f"{BASE}/api/pub/register/preview", json={
        "email": EMAIL("legacy"), "phone": "9000000030",
    }, timeout=30)
    assert r.json()["scenario"] == "shortlisted_not_scheduled"
