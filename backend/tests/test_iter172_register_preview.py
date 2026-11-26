"""
iter172 — Req 4 safe slice: /api/pub/register/preview endpoint.

READ-ONLY detection endpoint. These tests seed strictly-tagged throwaway
rows (uuid-suffixed) and tear them down narrowly. No live-data writes,
no outbound notifications touched.
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
EMAIL = lambda who: f"it172_{TAG}_{who}@example.test"  # noqa: E731


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    db.bb_registrations.delete_many({"email": {"$regex": f"^it172_{TAG}_"}})
    db.bb_registrations.delete_many({"_it172_tag": TAG})
    c.close()


def _preview(email: str, phone: str):
    return requests.post(
        f"{BASE}/api/pub/register/preview",
        json={"email": email, "phone": phone},
        timeout=30,
    )


def test_preview_scenario_none_for_unknown_identity():
    r = _preview(EMAIL("brandnew"), "9000000011")
    assert r.status_code == 200, r.text
    assert r.json()["scenario"] == "none"


def test_preview_blocked_by_attendance_for_recent_verified():
    e = EMAIL("blocked")
    two_months_ago = (datetime.now(timezone.utc) - relativedelta(months=2)).isoformat()
    c = MongoClient(MONGO_URL)
    c[DB_NAME].bb_registrations.insert_one({
        "email": e, "phone": "9000000012",
        "otp_verified": True, "otp_verified_at": two_months_ago,
        "last_update": two_months_ago,
        "_it172_tag": TAG,
    })
    c.close()
    r = _preview(e, "9000000012")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["scenario"] == "blocked_by_attendance"
    assert j.get("eligible_from"), f"eligible_from missing: {j}"


def test_preview_shortlisted_not_scheduled():
    e = EMAIL("shortonly")
    c = MongoClient(MONGO_URL)
    c[DB_NAME].bb_registrations.insert_one({
        "email": e, "phone": "9000000013",
        "job_role": "AI & ML Engineer",
        "is_shortlisted": True,
        "superseded": False,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        # No schedule_date / schedule_time => shortlisted but not scheduled
        "_it172_tag": TAG,
    })
    c.close()
    r = _preview(e, "9000000013")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["scenario"] == "shortlisted_not_scheduled", j
    assert j["existing_role"] == "AI & ML Engineer"


def test_preview_scheduled_returns_date_and_time():
    e = EMAIL("sched")
    c = MongoClient(MONGO_URL)
    c[DB_NAME].bb_registrations.insert_one({
        "email": e, "phone": "9000000014",
        "job_role": "HR Executive",
        "is_shortlisted": True,
        "superseded": False,
        "schedule_date": "2026-12-01",
        "schedule_time": "11:00",
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "_it172_tag": TAG,
    })
    c.close()
    r = _preview(e, "9000000014")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["scenario"] == "scheduled"
    assert j["existing_role"] == "HR Executive"
    assert j["schedule_date"] == "2026-12-01"
    assert j["schedule_time"] == "11:00"


def test_preview_phone_only_match_still_detects():
    """OR-match: querying with a different email but the same phone must
    still surface the existing application."""
    e_stored = EMAIL("phoneowner")
    phone = "9000000015"
    c = MongoClient(MONGO_URL)
    c[DB_NAME].bb_registrations.insert_one({
        "email": e_stored, "phone": phone,
        "job_role": "Data Engineer",
        "is_shortlisted": True,
        "superseded": False,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "_it172_tag": TAG,
    })
    c.close()
    r = _preview(EMAIL("otheremail"), phone)
    assert r.status_code == 200
    assert r.json()["scenario"] == "shortlisted_not_scheduled"


def test_preview_superseded_rows_are_ignored():
    """A superseded (old) shortlisted row must NOT surface as active."""
    e = EMAIL("superseded")
    c = MongoClient(MONGO_URL)
    c[DB_NAME].bb_registrations.insert_one({
        "email": e, "phone": "9000000016",
        "job_role": "Old Role",
        "is_shortlisted": True,
        "superseded": True,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "_it172_tag": TAG,
    })
    c.close()
    r = _preview(e, "9000000016")
    assert r.status_code == 200
    assert r.json()["scenario"] == "none"


def test_preview_is_read_only():
    """The endpoint must not insert / update / delete anything."""
    e = EMAIL("ro")
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    db.bb_registrations.insert_one({
        "email": e, "phone": "9000000017",
        "job_role": "AI & ML Engineer",
        "is_shortlisted": True,
        "superseded": False,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "_it172_tag": TAG,
    })
    before = db.bb_registrations.find_one({"email": e}, {"_id": 0})
    c.close()
    # Hit the endpoint 3 times
    for _ in range(3):
        r = _preview(e, "9000000017")
        assert r.status_code == 200
    c = MongoClient(MONGO_URL)
    after = c[DB_NAME].bb_registrations.find_one({"email": e}, {"_id": 0})
    c.close()
    assert before == after, "preview endpoint must be strictly read-only"
