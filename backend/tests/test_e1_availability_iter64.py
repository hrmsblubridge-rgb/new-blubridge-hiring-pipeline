"""Iter64 — independent verification of Enhancement 1 (Chennai availability gate,
withdrawal, reactivation, admin config, AI-calling eligibility, workers)."""
from __future__ import annotations

import os
import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE}/api"
CREATED_TOKENS: list = []
CREATED_PD_EMAILS: list = []


@pytest.fixture(scope="module")
def mdb():
    from pymongo import MongoClient
    c = MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=3000)
    db = c[os.environ["DB_NAME"]]
    assert db.name == "hiring_ai_testing"
    yield db
    if CREATED_TOKENS:
        db.bb_registrations.delete_many({"schedule_token": {"$in": CREATED_TOKENS}})
        db.bb_availability_responses.delete_many({"schedule_token": {"$in": CREATED_TOKENS}})
    if CREATED_PD_EMAILS:
        db.pipeline_data.delete_many({"email": {"$in": CREATED_PD_EMAILS}})
    c.close()


def _phone():
    return "7" + "".join(random.choice("0123456789") for _ in range(9))


@pytest.fixture
def mk(mdb):
    def _mk(**ov):
        tok = f"e1test-{uuid.uuid4().hex[:16]}"
        doc = {
            "form_id": "e1test-form", "full_name": "TEST_E1 Iter64",
            "email": f"{tok}@example.test", "phone": _phone(),
            "status": "Interview Not Scheduled", "is_shortlisted": True,
            "schedule_token": tok, "superseded": False,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        doc.update(ov)
        mdb.bb_registrations.insert_one(doc)
        CREATED_TOKENS.append(tok)
        return doc
    return _mk


@pytest.fixture(scope="module")
def admin():
    s = requests.Session()
    r = s.post(f"{API}/login", json={"username": "Admin User", "password": "QAZwsx##123"}, timeout=15)
    if r.status_code != 200:
        pytest.skip(f"login failed {r.status_code}")
    return s


def _avail(tok, **body):
    return requests.post(f"{API}/pub/availability/{tok}", json=body, timeout=15)


# ---- GET availability / config ----
class TestGet:
    def test_unknown_404(self):
        assert requests.get(f"{API}/pub/availability/nosuchtoken-xyz", timeout=15).status_code == 404
        assert _avail("nosuchtoken-xyz", availability=True).status_code == 404

    def test_reason_ids(self, mk):
        d = mk()
        r = requests.get(f"{API}/pub/availability/{d['schedule_token']}", timeout=15)
        assert r.status_code == 200
        j = r.json()
        ids = {o["id"] for o in j["config"]["reason_options"]}
        assert {"cannot_travel", "distance", "virtual_preferred", "work_commit",
                "timing", "not_interested", "other"} <= ids
        assert j["candidate"]["latest_availability"] is None
        assert "_id" not in j["config"]

    def test_null_compat_booked_row(self, mk):
        d = mk(status="Interview Scheduled", schedule_date="2030-01-10", schedule_time="10:00:00")
        j = requests.get(f"{API}/pub/availability/{d['schedule_token']}", timeout=15).json()
        assert j["candidate"]["latest_availability"] is None
        assert j["candidate"]["has_booking"] is True


# ---- POST availability validation ----
class TestSubmit:
    def test_yes_persists_and_history(self, mdb, mk):
        d = mk()
        r = _avail(d["schedule_token"], availability=True)
        assert r.status_code == 200
        reg = mdb.bb_registrations.find_one({"schedule_token": d["schedule_token"]})
        assert reg["chennai_availability_latest"] is True
        assert mdb.bb_availability_responses.count_documents({"schedule_token": d["schedule_token"]}) == 1

    def test_no_validation(self, mk):
        t = mk()["schedule_token"]
        assert _avail(t, availability=False).status_code == 400
        assert _avail(t, availability=False, reason_id="bogus").status_code == 400
        assert _avail(t, availability=False, reason_id="other").status_code == 400
        assert _avail(t, availability=False, reason_id="other", other_text="x" * 51).status_code == 422
        assert _avail(t, availability=False, reason_id="other", other_text="x" * 50).status_code == 200
        assert _avail(t, availability=False, reason_id="distance").status_code == 200

    def test_nonce_idempotent(self, mdb, mk):
        t = mk()["schedule_token"]
        nonce = f"e1test-nonce-{uuid.uuid4().hex}"
        for _ in range(3):
            assert _avail(t, availability=True, submission_nonce=nonce).status_code == 200
        assert mdb.bb_availability_responses.count_documents({"submission_nonce": nonce}) == 1

    def test_otp_untouched(self, mdb, mk):
        d = mk(otp_verified=False)
        t = d["schedule_token"]
        _avail(t, availability=False, reason_id="timing")
        reg = mdb.bb_registrations.find_one({"schedule_token": t})
        assert reg.get("otp_verified") is False


# ---- schedule gate ----
class TestScheduleGate:
    def _sched(self, t):
        return requests.post(f"{API}/pub/schedule/{t}", json={"date": "2030-02-11", "time": "10:00 AM"}, timeout=20)

    def test_null_blocks(self, mk):
        r = self._sched(mk()["schedule_token"])
        assert r.status_code == 409 and "availability" in r.json()["detail"].lower()

    def test_no_blocks(self, mk):
        t = mk()["schedule_token"]
        _avail(t, availability=False, reason_id="timing")
        r = self._sched(t)
        assert r.status_code == 409 and "availability" in r.json()["detail"].lower()

    def test_withdrawn_blocks(self, mk):
        t = mk(withdrawn_by_candidate=True, chennai_availability_latest=True)["schedule_token"]
        r = self._sched(t)
        assert r.status_code == 409 and "withdrawn" in r.json()["detail"].lower()

    def test_yes_passes_gate(self, mk):
        t = mk()["schedule_token"]
        _avail(t, availability=True)
        r = self._sched(t)
        # Gate must not produce availability/withdrawn 409. Other business validation may apply.
        if r.status_code == 409:
            det = r.json().get("detail", "").lower()
            assert "availability" not in det and "withdrawn" not in det, det
        print("schedule after YES:", r.status_code, r.text[:200])


# ---- booked YES→NO HR review + journey ----
class TestBookedFlip:
    def test_flip_and_journey(self, mdb, mk, admin):
        d = mk(status="Interview Scheduled", schedule_date="2030-03-05", schedule_time="11:00:00")
        t = d["schedule_token"]
        mdb.pipeline_data.insert_one({"email": d["email"], "phone": d["phone"], "name": "TEST_E1 Iter64",
                                      "status": "Interview Scheduled"})
        CREATED_PD_EMAILS.append(d["email"])
        assert _avail(t, availability=True).status_code == 200
        assert _avail(t, availability=False, reason_id="work_commit").status_code == 200
        reg = mdb.bb_registrations.find_one({"schedule_token": t})
        assert reg["schedule_date"] == "2030-03-05" and reg["schedule_time"] == "11:00:00"
        assert reg["status"] == "Interview Scheduled"
        assert reg["needs_hr_review"] is True and reg["needs_hr_review_reason"] == "booked_changed_to_no"
        r = admin.get(f"{API}/bb/candidate-journey", params={"email": d["email"]}, timeout=20)
        assert r.status_code == 200, r.text[:300]
        ab = r.json().get("availability_block")
        assert ab and ab["needs_hr_review"] is True and ab["latest_availability"] is False
        assert len(ab["history"]) >= 2
        assert _avail(t, availability=True).status_code == 200
        reg = mdb.bb_registrations.find_one({"schedule_token": t})
        assert reg["needs_hr_review"] is False


# ---- withdraw / reactivate ----
class TestWithdrawReactivate:
    def test_withdraw_flow(self, mdb, mk):
        t = mk()["schedule_token"]
        assert requests.post(f"{API}/pub/availability/{t}/withdraw", json={"confirmed": True}, timeout=15).status_code == 409
        _avail(t, availability=False, reason_id="not_interested")
        assert requests.post(f"{API}/pub/availability/{t}/withdraw", json={"confirmed": False}, timeout=15).status_code == 400
        r1 = requests.post(f"{API}/pub/availability/{t}/withdraw", json={"confirmed": True}, timeout=15)
        assert r1.status_code == 200
        r2 = requests.post(f"{API}/pub/availability/{t}/withdraw", json={"confirmed": True}, timeout=15)
        assert r2.status_code == 200 and r2.json().get("idempotent") is True
        reg = mdb.bb_registrations.find_one({"schedule_token": t})
        assert reg["withdrawn_by_candidate"] is True and reg["withdrawn_at"]
        assert reg["withdrawal_reason_id"] == "not_interested"
        assert mdb.bb_availability_responses.count_documents({"schedule_token": t, "response_type": "withdrawal"}) == 1
        # availability POST blocked while withdrawn
        assert _avail(t, availability=True).status_code == 409

    def test_reactivate(self, mdb, mk):
        t = mk()["schedule_token"]
        assert requests.post(f"{API}/pub/availability/{t}/reactivate", json={"choice": "yes"}, timeout=15).status_code == 409
        mdb.bb_registrations.update_one({"schedule_token": t}, {"$set": {"withdrawn_by_candidate": True, "chennai_availability_latest": False}})
        assert requests.post(f"{API}/pub/availability/{t}/reactivate", json={"choice": "other"}, timeout=15).status_code == 400
        assert requests.post(f"{API}/pub/availability/{t}/reactivate", json={"choice": "other", "other_text": "TEST_q"}, timeout=15).status_code == 200
        assert requests.post(f"{API}/pub/availability/{t}/reactivate", json={"choice": "maybe"}, timeout=15).status_code == 422
        r = requests.post(f"{API}/pub/availability/{t}/reactivate", json={"choice": "yes"}, timeout=15)
        assert r.status_code == 200
        reg = mdb.bb_registrations.find_one({"schedule_token": t})
        assert reg["withdrawn_by_candidate"] is False and reg["chennai_availability_latest"] is True
        assert reg["reactivation_count"] == 1 and reg["reactivated_at"]

    @pytest.mark.parametrize("ov", [{"status": "Rejected"}, {"is_shortlisted": False}])
    def test_company_rejection(self, mk, ov):
        t = mk(withdrawn_by_candidate=True, chennai_availability_latest=False, **ov)["schedule_token"]
        r = requests.post(f"{API}/pub/availability/{t}/reactivate", json={"choice": "yes"}, timeout=15)
        assert r.status_code == 409 and "no longer eligible" in r.json()["detail"]


# ---- admin ----
class TestAdmin:
    def test_auth_required(self):
        for p in ["current", "draft", "versions", "audit"]:
            assert requests.get(f"{API}/bb/availability-question/{p}", timeout=15).status_code in (401, 403)
        assert requests.post(f"{API}/bb/availability-question/publish", json={"config": {}}, timeout=15).status_code in (401, 403)

    def test_reserved_ids(self, admin):
        cfg = admin.get(f"{API}/bb/availability-question/current", timeout=15).json()
        cfg["reason_options"] = [o for o in cfg["reason_options"] if o["id"] != "distance"]
        r = admin.post(f"{API}/bb/availability-question/draft", json={"config": cfg}, timeout=15)
        assert r.status_code == 400 and "reserved reason IDs must remain" in r.json()["detail"]

    def test_publish_same_config(self, mdb, admin):
        cfg = admin.get(f"{API}/bb/availability-question/current", timeout=15).json()
        v0 = cfg["version"]
        n_audit = mdb.bb_availability_question_audit.count_documents({})
        r = admin.post(f"{API}/bb/availability-question/publish", json={"config": cfg}, timeout=15)
        assert r.status_code == 200 and r.json()["version"] == v0 + 1
        assert admin.get(f"{API}/bb/availability-question/current", timeout=15).json()["version"] == v0 + 1
        assert mdb.bb_availability_question_versions.count_documents({"version": v0 + 1}) == 1
        assert mdb.bb_availability_question_audit.count_documents({}) == n_audit + 1
        dr = admin.get(f"{API}/bb/availability-question/draft", timeout=15).json()
        assert dr["based_on_version"] == v0 + 1
        vs = admin.get(f"{API}/bb/availability-question/versions", timeout=15).json()["versions"]
        assert vs[0]["version"] == v0 + 1


# ---- AI calling eligibility ----
class TestEligibility:
    BASE = {"status": "Interview Scheduled", "is_shortlisted": True, "phone": "9876543210",
            "schedule_date": "2030-01-01", "schedule_time": "10:00:00",
            "ai_call_consent": {"status": "granted"}}

    def _e(self, **ov):
        from ai_calling.dryrun import eligibility
        return eligibility({**self.BASE, **ov})

    def test_null_and_true_eligible(self):
        assert self._e() == []
        assert self._e(chennai_availability_latest=None) == []
        assert self._e(chennai_availability_latest=True) == []

    def test_withdrawn_and_no(self):
        assert self._e(withdrawn_by_candidate=True) == ["WITHDRAWN"]
        assert self._e(chennai_availability_latest=False) == ["LATEST_AVAILABILITY_NO"]

    def test_ordering(self):
        r = self._e(superseded=True, withdrawn_by_candidate=True, chennai_availability_latest=False,
                    otp_verified=True, do_not_call=True, ai_call_consent={})
        assert r[:3] == ["SUPERSEDED", "WITHDRAWN", "LATEST_AVAILABILITY_NO"]
        assert r.index("ATTENDED") < r.index("DO_NOT_CALL") < r.index("NO_CALLING_CONSENT")


# ---- worker filters (source inspection) ----
def test_worker_filters_source():
    src = open(os.path.join(os.path.dirname(__file__), "..", "bg_workers.py")).read()
    assert src.count('"withdrawn_by_candidate": {"$ne": True}') >= 2
    assert '"chennai_availability_latest"' not in src
