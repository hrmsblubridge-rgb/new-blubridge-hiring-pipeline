"""Independent review (testing agent) of Enhancement 1 iter65 fixes.

Covers concurrency/idempotency edge cases, OTP exposure, employee OTP flow,
atomic publish under heavier concurrency, worker source assertions,
and the credentials-doc contract.
"""
from __future__ import annotations

import os
import random
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE}/api"


def _env_val(key):
    v = os.environ.get(key)
    if v:
        return v
    for line in Path("/app/backend/.env").read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.partition("=")[2].strip().strip('"').strip("'")
    return None


@pytest.fixture(scope="session")
def mdb():
    from pymongo import MongoClient
    db_name = _env_val("DB_NAME")
    assert db_name == "hiring_ai_testing", "SAFETY: refusing to run outside hiring_ai_testing"
    client = MongoClient(_env_val("MONGO_URL"), serverSelectionTimeoutMS=3000)
    yield client[db_name]
    client.close()


@pytest.fixture(scope="session")
def admin():
    pw = _env_val("TEST_ADMIN_PASSWORD")
    if not pw:
        pytest.skip("TEST_ADMIN_PASSWORD not set")
    s = requests.Session()
    r = s.post(f"{API}/login", json={"username": "Admin User", "password": pw}, timeout=15)
    if r.status_code != 200:
        pytest.skip("admin login failed")
    return s


@pytest.fixture
def reg(mdb):
    toks = []

    def _mk(**over):
        tok = f"iter65-{uuid.uuid4().hex[:16]}"
        phone = "0000" + "".join(random.choice("0123456789") for _ in range(6))
        doc = {"form_id": "iter65-ta-form", "full_name": "TEST_iter65_ta",
               "email": f"{tok}@test.local", "phone": phone,
               "status": "Interview Not Scheduled", "is_shortlisted": True,
               "schedule_token": tok, "superseded": False,
               "registered_at": datetime.now(timezone.utc).isoformat()}
        doc.update(over)
        mdb.bb_registrations.insert_one(doc)
        toks.append(tok)
        return doc

    yield _mk
    if toks:
        mdb.bb_registrations.delete_many({"schedule_token": {"$in": toks}})
        mdb.bb_availability_responses.delete_many({"schedule_token": {"$in": toks}})


def _post(path, body):
    return requests.post(f"{API}{path}", json=body, timeout=20)


# ---- OTP exposure + employee OTP flow ----
class TestOtp:
    def test_public_schedule_no_otp_and_employee_verify(self, reg, mdb, admin):
        r0 = reg(chennai_availability_latest=True)
        d = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
        r = _post(f"/pub/schedule/{r0['schedule_token']}", {"date": d, "time": "11:00:00"})
        assert r.status_code == 200, r.text
        assert "otp" not in r.json()
        assert "otp" not in r.text.lower() or '"otp"' not in r.text
        stored = mdb.bb_registrations.find_one({"_id": r0["_id"]})
        assert stored.get("otp")
        # unauthenticated verify must be rejected
        ua = _post("/bb/verify-otp", {"phone": r0["phone"], "otp": stored["otp"]})
        assert ua.status_code in (401, 403)
        # authenticated verify marks attendance
        v = admin.post(f"{API}/bb/verify-otp", json={"phone": r0["phone"], "otp": stored["otp"]}, timeout=15)
        assert v.status_code == 200 and v.json().get("success") is True, v.text
        after = mdb.bb_registrations.find_one({"_id": r0["_id"]})
        assert after["otp_verified"] is True and after["status"] == "Attended"

    def test_schedule_blocked_without_yes(self, reg):
        r0 = reg()
        d = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
        r = _post(f"/pub/schedule/{r0['schedule_token']}", {"date": d, "time": "11:00:00"})
        assert r.status_code == 409


# ---- Idempotency ----
class TestIdempotency:
    def test_concurrent_same_nonce_availability(self, reg, mdb):
        r0 = reg()
        body = {"availability": False, "reason_id": "distance", "submission_nonce": uuid.uuid4().hex}
        with ThreadPoolExecutor(8) as ex:
            res = list(ex.map(lambda _: _post(f"/pub/availability/{r0['schedule_token']}", body), range(8)))
        assert all(x.status_code == 200 for x in res), [x.status_code for x in res]
        assert mdb.bb_availability_responses.count_documents({"schedule_token": r0["schedule_token"]}) == 1

    def test_other_text_51_chars_422(self, reg):
        r0 = reg()
        r = _post(f"/pub/availability/{r0['schedule_token']}",
                  {"availability": False, "reason_id": "other", "other_text": "x" * 51})
        assert r.status_code == 422

    def test_withdraw_replay_and_concurrent(self, reg, mdb):
        r0 = reg()
        t = r0["schedule_token"]
        assert _post(f"/pub/availability/{t}", {"availability": False, "reason_id": "timing",
                                                 "submission_nonce": uuid.uuid4().hex}).status_code == 200
        n = uuid.uuid4().hex
        with ThreadPoolExecutor(4) as ex:
            res = list(ex.map(lambda _: _post(f"/pub/availability/{t}/withdraw",
                                              {"confirmed": True, "submission_nonce": n}), range(4)))
        assert all(x.status_code == 200 for x in res)
        assert sum(1 for x in res if x.json().get("idempotent")) == 3
        assert mdb.bb_availability_responses.count_documents({"schedule_token": t, "response_type": "withdrawal"}) == 1
        assert mdb.bb_registrations.find_one({"schedule_token": t})["withdrawn_by_candidate"] is True

    def test_reactivate_yes_concurrent_same_nonce(self, reg, mdb):
        r0 = reg(withdrawn_by_candidate=True, chennai_availability_latest=False)
        t = r0["schedule_token"]
        n = uuid.uuid4().hex
        with ThreadPoolExecutor(4) as ex:
            res = list(ex.map(lambda _: _post(f"/pub/availability/{t}/reactivate",
                                              {"choice": "yes", "submission_nonce": n}), range(4)))
        assert all(x.status_code == 200 for x in res), [x.status_code for x in res]
        doc = mdb.bb_registrations.find_one({"schedule_token": t})
        assert doc["reactivation_count"] == 1 and doc["withdrawn_by_candidate"] is False
        assert mdb.bb_availability_responses.count_documents({"schedule_token": t, "response_type": "reactivation"}) == 1
        # sequential replay after success → idempotent, not 409
        rp = _post(f"/pub/availability/{t}/reactivate", {"choice": "yes", "submission_nonce": n})
        assert rp.status_code == 200 and rp.json().get("idempotent") is True

    def test_cross_type_nonce_reuse_does_not_mutate_state(self, reg, mdb):
        """A nonce already used for an availability row, reused on /withdraw,
        must not flip withdrawn_by_candidate and then 409 (state/history divergence)."""
        r0 = reg()
        t = r0["schedule_token"]
        n = uuid.uuid4().hex
        assert _post(f"/pub/availability/{t}", {"availability": False, "reason_id": "timing",
                                                 "submission_nonce": n}).status_code == 200
        w = _post(f"/pub/availability/{t}/withdraw", {"confirmed": True, "submission_nonce": n})
        doc = mdb.bb_registrations.find_one({"schedule_token": t})
        wrows = mdb.bb_availability_responses.count_documents({"schedule_token": t, "response_type": "withdrawal"})
        # Either rejected without side-effects, or accepted WITH a history row.
        if w.status_code == 409:
            assert doc.get("withdrawn_by_candidate") is not True, "409 returned but registration WAS withdrawn"
        else:
            assert wrows == 1


# ---- Atomic publish ----
class TestPublish:
    def test_concurrent_publish_distinct_versions(self, admin, mdb):
        cur = admin.get(f"{API}/bb/availability-question/current", timeout=15).json()
        cfg = {k: v for k, v in cur.items() if k not in ("version", "published_at", "published_by", "snapshot_at")}
        audit_before = mdb.bb_availability_question_audit.count_documents({"action": "publish"})
        N = 6
        with ThreadPoolExecutor(N) as ex:
            res = list(ex.map(lambda _: admin.post(f"{API}/bb/availability-question/publish",
                                                   json={"config": cfg}, timeout=30), range(N)))
        codes = [r.status_code for r in res]
        assert all(c == 200 for c in codes), codes
        vers = sorted(r.json()["version"] for r in res)
        assert len(set(vers)) == N
        assert mdb.bb_availability_question_versions.count_documents({"version": {"$in": vers}}) == N
        assert mdb.bb_availability_question_audit.count_documents({"action": "publish"}) == audit_before + N
        assert mdb.bb_availability_question_config.find_one({"_id": "current"})["version"] == max(vers)


# ---- Source assertions ----
def test_worker_filters_source():
    src = Path("/app/backend/bg_workers.py").read_text()
    assert src.count('"withdrawn_by_candidate": {"$ne": True}') >= 2
    assert "chennai_availability_latest" not in src


def test_credentials_doc_contract():
    doc = Path("/app/memory/test_credentials.md").read_text()
    pw = _env_val("TEST_ADMIN_PASSWORD")
    assert pw and pw not in doc, "plaintext password present in credentials doc"
    assert "TEST_ADMIN_PASSWORD" in doc, "credentials doc must reference TEST_ADMIN_PASSWORD env var"
