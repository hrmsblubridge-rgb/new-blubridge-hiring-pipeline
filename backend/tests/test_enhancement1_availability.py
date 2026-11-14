"""Enhancement 1 — Chennai in-person availability, withdrawal and reactivation.

Backend regression suite. Uses the preview Mongo (hiring_ai_testing). Creates
scoped `bb_registrations` rows with a unique `schedule_token` per test and
cleans them up at the end. Never touches real candidates, OTP values, bookings
or AiSensy/email providers.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE}/api"


def _tok() -> str:
    return f"e1test-{uuid.uuid4().hex[:16]}"


@pytest.fixture(scope="session")
def mdb():
    from pymongo import MongoClient
    url = os.environ.get("MONGO_URL") or "mongodb://localhost:27017"
    db = os.environ.get("DB_NAME") or "hiring_ai_testing"
    client = MongoClient(url, serverSelectionTimeoutMS=2000)
    yield client[db]
    client.close()


@pytest.fixture
def seed_reg(mdb):
    """Create a minimal bb_registrations row and yield its doc.

    Cleans up via narrow filter on the test token at teardown.
    """
    created_tokens: list[str] = []

    def _mk(**overrides) -> dict:
        tok = _tok()
        doc = {
            "form_id": "e1test-form",
            "full_name": "E1 Test Candidate",
            "email": f"{tok}@example.test",
            "phone": "9000000001",
            "status": "Interview Not Scheduled",
            "is_shortlisted": True,
            "schedule_token": tok,
            "superseded": False,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        doc.update(overrides)
        mdb.bb_registrations.insert_one(doc)
        created_tokens.append(tok)
        return doc

    yield _mk

    if created_tokens:
        mdb.bb_registrations.delete_many({"schedule_token": {"$in": created_tokens}})
        mdb.bb_availability_responses.delete_many({"schedule_token": {"$in": created_tokens}})


def _admin_password() -> str:
    pw = os.environ.get("TEST_ADMIN_PASSWORD")
    if not pw:
        try:
            from pathlib import Path
            for line in Path("/app/backend/.env").read_text().splitlines():
                if line.startswith("TEST_ADMIN_PASSWORD="):
                    pw = line.partition("=")[2].strip().strip('"').strip("'")
                    break
        except Exception:
            pw = None
    if not pw:
        pytest.skip("TEST_ADMIN_PASSWORD env var not set")
    return pw


@pytest.fixture(scope="session")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/login",
               json={"username": "Admin User", "password": _admin_password()},
               timeout=10)
    assert r.status_code == 200, r.text
    return s


# =====================================================================
# Phase A/B — public token endpoints
# =====================================================================
class TestPublicAvailabilityRead:
    def test_unknown_token_404(self):
        r = requests.get(f"{API}/pub/availability/nosuchtoken", timeout=10)
        assert r.status_code == 404

    def test_new_candidate_returns_null_latest(self, seed_reg):
        reg = seed_reg()
        r = requests.get(f"{API}/pub/availability/{reg['schedule_token']}", timeout=10)
        assert r.status_code == 200
        body = r.json()
        # Config schema sanity (admins may freely edit text; we assert structure).
        assert isinstance(body["config"].get("main_question"), str) and body["config"]["main_question"].strip()
        assert isinstance(body["config"].get("reason_options"), list)
        assert body["candidate"]["latest_availability"] is None
        assert body["candidate"]["has_booking"] is False
        assert body["candidate"]["withdrawn"] is False
        # Fixed reason IDs must be exposed to the client.
        ids = {o["id"] for o in body["config"]["reason_options"]}
        assert {"cannot_travel", "distance", "other"} <= ids

    def test_existing_booking_null_availability_preserved(self, seed_reg):
        reg = seed_reg(status="Interview Scheduled", schedule_date="2099-12-31",
                       schedule_time="11:00:00")
        r = requests.get(f"{API}/pub/availability/{reg['schedule_token']}", timeout=10)
        body = r.json()
        assert body["candidate"]["has_booking"] is True
        assert body["candidate"]["booking"]["schedule_date"] == "2099-12-31"
        assert body["candidate"]["latest_availability"] is None


class TestPublicAvailabilitySubmit:
    def test_yes_unlocks_scheduling(self, mdb, seed_reg):
        reg = seed_reg()
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}",
                          json={"availability": True}, timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body["candidate"]["latest_availability"] is True
        d = mdb.bb_registrations.find_one({"_id": reg["_id"]})
        assert d["chennai_availability_latest"] is True
        hist = list(mdb.bb_availability_responses.find({"schedule_token": reg["schedule_token"]}))
        assert len(hist) == 1
        assert hist[0]["response_type"] == "availability"
        assert hist[0]["availability"] is True

    def test_no_requires_reason(self, seed_reg):
        reg = seed_reg()
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}",
                          json={"availability": False}, timeout=10)
        assert r.status_code == 400

    def test_no_with_invalid_reason_rejected(self, seed_reg):
        reg = seed_reg()
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}",
                          json={"availability": False, "reason_id": "frog"}, timeout=10)
        assert r.status_code == 400

    def test_no_with_predefined_reason_saves(self, mdb, seed_reg):
        reg = seed_reg()
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}",
                          json={"availability": False, "reason_id": "distance"}, timeout=10)
        assert r.status_code == 200
        d = mdb.bb_registrations.find_one({"_id": reg["_id"]})
        assert d["chennai_availability_latest"] is False
        assert d["chennai_availability_reason_id"] == "distance"
        assert d["chennai_availability_reason_text"].startswith("Distance")
        assert d.get("chennai_availability_other_text") is None

    def test_other_requires_text(self, seed_reg):
        reg = seed_reg()
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}",
                          json={"availability": False, "reason_id": "other"}, timeout=10)
        assert r.status_code == 400

    def test_other_text_clipped_at_50_backend(self, seed_reg):
        reg = seed_reg()
        long = "x" * 60
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}",
                          json={"availability": False, "reason_id": "other", "other_text": long},
                          timeout=10)
        assert r.status_code == 422  # pydantic validator

    def test_other_text_valid(self, mdb, seed_reg):
        reg = seed_reg()
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}",
                          json={"availability": False, "reason_id": "other",
                                "other_text": "Overseas assignment"}, timeout=10)
        assert r.status_code == 200
        d = mdb.bb_registrations.find_one({"_id": reg["_id"]})
        assert d["chennai_availability_other_text"] == "Overseas assignment"

    def test_history_appends_each_submission(self, mdb, seed_reg):
        reg = seed_reg()
        for _ in range(3):
            requests.post(f"{API}/pub/availability/{reg['schedule_token']}",
                          json={"availability": True}, timeout=10)
        count = mdb.bb_availability_responses.count_documents(
            {"schedule_token": reg["schedule_token"]}
        )
        assert count == 3

    def test_booked_yes_to_no_raises_hr_review(self, mdb, seed_reg):
        reg = seed_reg(status="Interview Scheduled",
                       schedule_date="2099-12-31", schedule_time="11:00:00",
                       chennai_availability_latest=True,
                       chennai_availability_latest_at=datetime.now(timezone.utc).isoformat())
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}",
                          json={"availability": False, "reason_id": "work_commit"},
                          timeout=10)
        assert r.status_code == 200
        d = mdb.bb_registrations.find_one({"_id": reg["_id"]})
        assert d["chennai_availability_latest"] is False
        assert d.get("needs_hr_review") is True
        assert d.get("needs_hr_review_reason") == "booked_changed_to_no"
        # Booking is preserved.
        assert d["schedule_date"] == "2099-12-31"
        assert d["status"] == "Interview Scheduled"

    def test_booked_no_back_to_yes_clears_review(self, mdb, seed_reg):
        reg = seed_reg(status="Interview Scheduled",
                       schedule_date="2099-12-31", schedule_time="11:00:00",
                       needs_hr_review=True, needs_hr_review_reason="booked_changed_to_no",
                       chennai_availability_latest=False)
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}",
                          json={"availability": True}, timeout=10)
        assert r.status_code == 200
        d = mdb.bb_registrations.find_one({"_id": reg["_id"]})
        assert d["chennai_availability_latest"] is True
        assert d["needs_hr_review"] is False


# =====================================================================
# Scheduling gate — the biggest safety-net
# =====================================================================
class TestSchedulingGate:
    def test_no_availability_blocks_booking(self, seed_reg):
        reg = seed_reg()
        # No availability submitted yet.
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        r = requests.post(f"{API}/pub/schedule/{reg['schedule_token']}",
                          json={"date": tomorrow, "time": "11:00:00"}, timeout=10)
        assert r.status_code == 409
        assert "availability" in r.json()["detail"].lower()

    def test_no_answer_blocks_booking(self, seed_reg):
        reg = seed_reg()
        requests.post(f"{API}/pub/availability/{reg['schedule_token']}",
                      json={"availability": False, "reason_id": "distance"}, timeout=10)
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        r = requests.post(f"{API}/pub/schedule/{reg['schedule_token']}",
                          json={"date": tomorrow, "time": "11:00:00"}, timeout=10)
        assert r.status_code == 409

    def test_withdrawn_blocks_booking(self, seed_reg):
        reg = seed_reg(chennai_availability_latest=False,
                       withdrawn_by_candidate=True,
                       withdrawn_at=datetime.now(timezone.utc).isoformat())
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        r = requests.post(f"{API}/pub/schedule/{reg['schedule_token']}",
                          json={"date": tomorrow, "time": "11:00:00"}, timeout=10)
        assert r.status_code == 409
        assert "withdrawn" in r.json()["detail"].lower()


# =====================================================================
# Withdrawal
# =====================================================================
class TestWithdrawal:
    def test_withdraw_requires_no_first(self, seed_reg):
        reg = seed_reg(chennai_availability_latest=True,
                       chennai_availability_latest_at=datetime.now(timezone.utc).isoformat())
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}/withdraw",
                          json={"confirmed": True}, timeout=10)
        assert r.status_code == 409

    def test_withdraw_requires_confirmed(self, seed_reg):
        reg = seed_reg(chennai_availability_latest=False)
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}/withdraw",
                          json={"confirmed": False}, timeout=10)
        assert r.status_code == 400

    def test_happy_path_withdrawal(self, mdb, seed_reg):
        reg = seed_reg(chennai_availability_latest=False,
                       chennai_availability_reason_id="not_interested",
                       chennai_availability_reason_text="Not interested in this opportunity.")
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}/withdraw",
                          json={"confirmed": True}, timeout=10)
        assert r.status_code == 200
        d = mdb.bb_registrations.find_one({"_id": reg["_id"]})
        assert d["withdrawn_by_candidate"] is True
        assert d["withdrawal_reason_id"] == "not_interested"
        hist = mdb.bb_availability_responses.find_one(
            {"schedule_token": reg["schedule_token"], "response_type": "withdrawal"}
        )
        assert hist is not None
        assert hist["withdraw_confirmed"] is True

    def test_withdraw_is_idempotent(self, mdb, seed_reg):
        reg = seed_reg(chennai_availability_latest=False,
                       withdrawn_by_candidate=True,
                       withdrawn_at=datetime.now(timezone.utc).isoformat())
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}/withdraw",
                          json={"confirmed": True}, timeout=10)
        assert r.status_code == 200
        assert r.json().get("idempotent") is True


# =====================================================================
# Reactivation
# =====================================================================
class TestReactivation:
    def test_reactivation_only_for_withdrawn(self, seed_reg):
        reg = seed_reg()
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}/reactivate",
                          json={"choice": "yes"}, timeout=10)
        assert r.status_code == 409

    def test_reactivation_yes_resets_latest_to_true(self, mdb, seed_reg):
        reg = seed_reg(withdrawn_by_candidate=True,
                       withdrawn_at=datetime.now(timezone.utc).isoformat(),
                       chennai_availability_latest=False,
                       is_shortlisted=True)
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}/reactivate",
                          json={"choice": "yes"}, timeout=10)
        assert r.status_code == 200
        d = mdb.bb_registrations.find_one({"_id": reg["_id"]})
        assert d["withdrawn_by_candidate"] is False
        assert d["chennai_availability_latest"] is True
        assert d["reactivation_count"] == 1

    def test_reactivation_no_keeps_withdrawn(self, mdb, seed_reg):
        reg = seed_reg(withdrawn_by_candidate=True,
                       withdrawn_at=datetime.now(timezone.utc).isoformat())
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}/reactivate",
                          json={"choice": "no"}, timeout=10)
        assert r.status_code == 200
        d = mdb.bb_registrations.find_one({"_id": reg["_id"]})
        assert d["withdrawn_by_candidate"] is True

    def test_reactivation_other_requires_text(self, seed_reg):
        reg = seed_reg(withdrawn_by_candidate=True,
                       withdrawn_at=datetime.now(timezone.utc).isoformat())
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}/reactivate",
                          json={"choice": "other"}, timeout=10)
        assert r.status_code == 400

    def test_company_rejection_blocks_reactivation(self, seed_reg):
        reg = seed_reg(withdrawn_by_candidate=True,
                       withdrawn_at=datetime.now(timezone.utc).isoformat(),
                       status="Rejected", is_shortlisted=False)
        r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}/reactivate",
                          json={"choice": "yes"}, timeout=10)
        assert r.status_code == 409


# =====================================================================
# Phase D — admin config
# =====================================================================
class TestAdminConfig:
    def test_admin_get_current(self, admin_session):
        r = admin_session.get(f"{API}/bb/availability-question/current", timeout=10)
        assert r.status_code == 200
        cfg = r.json()
        # Admins can edit text freely; assert structural integrity instead.
        assert isinstance(cfg.get("main_question"), str) and cfg["main_question"].strip()
        assert isinstance(cfg.get("reason_options"), list) and cfg["reason_options"]
        assert cfg["version"] >= 1

    def test_unauth_cannot_read_current(self):
        r = requests.get(f"{API}/bb/availability-question/current", timeout=10)
        assert r.status_code in (401, 403)

    def test_cannot_drop_reserved_reason_ids(self, admin_session):
        cur = admin_session.get(f"{API}/bb/availability-question/current", timeout=10).json()
        bad = dict(cur)
        # Drop "other" — forbidden.
        bad["reason_options"] = [o for o in cur["reason_options"] if o["id"] != "other"]
        r = admin_session.post(f"{API}/bb/availability-question/draft",
                               json={"config": bad}, timeout=10)
        assert r.status_code == 400
        assert "other" in r.text

    def test_draft_save_and_fetch(self, admin_session):
        cur = admin_session.get(f"{API}/bb/availability-question/current", timeout=10).json()
        draft = dict(cur)
        draft["main_question"] = "Updated question?"
        r = admin_session.post(f"{API}/bb/availability-question/draft",
                               json={"config": draft}, timeout=10)
        assert r.status_code == 200
        r2 = admin_session.get(f"{API}/bb/availability-question/draft", timeout=10)
        assert r2.status_code == 200
        assert r2.json()["draft"]["main_question"] == "Updated question?"


# =====================================================================
# Phase E — AI calling eligibility hooks
# =====================================================================
class TestEligibilityHooks:
    def test_withdrawn_adds_exclusion(self):
        from ai_calling.dryrun import eligibility
        b = {"status": "Interview Scheduled", "is_shortlisted": True,
             "schedule_date": "2099-12-31", "schedule_time": "11:00:00",
             "phone": "9000000001",
             "ai_call_consent": {"status": "granted"},
             "withdrawn_by_candidate": True}
        reasons = eligibility(b)
        assert "WITHDRAWN" in reasons

    def test_latest_no_adds_exclusion(self):
        from ai_calling.dryrun import eligibility
        b = {"status": "Interview Scheduled", "is_shortlisted": True,
             "schedule_date": "2099-12-31", "schedule_time": "11:00:00",
             "phone": "9000000001",
             "ai_call_consent": {"status": "granted"},
             "chennai_availability_latest": False}
        reasons = eligibility(b)
        assert "LATEST_AVAILABILITY_NO" in reasons

    def test_null_availability_is_not_an_exclusion(self):
        from ai_calling.dryrun import eligibility
        b = {"status": "Interview Scheduled", "is_shortlisted": True,
             "schedule_date": "2099-12-31", "schedule_time": "11:00:00",
             "phone": "9000000001",
             "ai_call_consent": {"status": "granted"},
             "chennai_availability_latest": None}
        reasons = eligibility(b)
        assert "LATEST_AVAILABILITY_NO" not in reasons
        assert "WITHDRAWN" not in reasons

    def test_yes_is_not_an_exclusion(self):
        from ai_calling.dryrun import eligibility
        b = {"status": "Interview Scheduled", "is_shortlisted": True,
             "schedule_date": "2099-12-31", "schedule_time": "11:00:00",
             "phone": "9000000001",
             "ai_call_consent": {"status": "granted"},
             "chennai_availability_latest": True}
        reasons = eligibility(b)
        assert reasons == []
