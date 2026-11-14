"""Enhancement 1 — iter65 corrections.

Covers:
  1. POST /api/pub/schedule/{token} does NOT return the OTP.
  2. Availability submissions are idempotent by `submission_nonce`
     (replay returns idempotent:true, no duplicate history row).
  3. Withdraw / reactivate are idempotent by nonce and by state.
  4. Nonce reuse with a different payload is rejected 409.
  5. Admin publish allocates monotonically-increasing version even when
     two publishes race; neither drops.
  6. Workers filter behaves per spec (NO-no-withdrawal still eligible,
     withdrawn excluded).
"""
from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE}/api"


def _admin_password() -> str:
    pw = os.environ.get("TEST_ADMIN_PASSWORD")
    if not pw and Path("/app/backend/.env").exists():
        for line in Path("/app/backend/.env").read_text().splitlines():
            if line.startswith("TEST_ADMIN_PASSWORD="):
                pw = line.partition("=")[2].strip().strip('"').strip("'")
                break
    if not pw:
        pytest.skip("TEST_ADMIN_PASSWORD env var not set")
    return pw


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
    tokens = []

    def _mk(**over):
        tok = f"iter65-{uuid.uuid4().hex[:16]}"
        doc = {
            "form_id": "iter65-form", "full_name": "Iter65",
            "email": f"{tok}@test.local", "phone": "9000012345",
            "status": "Interview Not Scheduled", "is_shortlisted": True,
            "schedule_token": tok, "superseded": False,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        doc.update(over)
        mdb.bb_registrations.insert_one(doc)
        tokens.append(tok)
        return doc

    yield _mk

    if tokens:
        mdb.bb_registrations.delete_many({"schedule_token": {"$in": tokens}})
        mdb.bb_availability_responses.delete_many({"schedule_token": {"$in": tokens}})


@pytest.fixture(scope="session")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/login",
               json={"username": "Admin User", "password": _admin_password()},
               timeout=10)
    assert r.status_code == 200
    return s


# =====================================================================
# TASK 3 — OTP exposure removed
# =====================================================================
def test_schedule_response_does_not_leak_otp(seed_reg, mdb):
    reg = seed_reg(chennai_availability_latest=True,
                   chennai_availability_latest_at=datetime.now(timezone.utc).isoformat())
    future_date = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    r = requests.post(f"{API}/pub/schedule/{reg['schedule_token']}",
                      json={"date": future_date, "time": "11:00:00"}, timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "otp" not in body, f"OTP leaked in public response: {body.keys()}"
    assert body.get("success") is True
    # OTP is still stored for employee verification.
    d = mdb.bb_registrations.find_one({"_id": reg["_id"]}, {"otp": 1})
    assert d.get("otp"), "OTP should still be stored for /bb/verify-otp"


# =====================================================================
# TASK 2 — submission_nonce idempotency
# =====================================================================
class TestIdempotencyAvailability:
    def test_same_nonce_replay(self, mdb, seed_reg):
        reg = seed_reg()
        nonce = uuid.uuid4().hex
        body = {"availability": True, "submission_nonce": nonce}
        r1 = requests.post(f"{API}/pub/availability/{reg['schedule_token']}", json=body, timeout=10)
        r2 = requests.post(f"{API}/pub/availability/{reg['schedule_token']}", json=body, timeout=10)
        assert r1.status_code == 200 and r2.status_code == 200
        assert r2.json().get("idempotent") is True
        cnt = mdb.bb_availability_responses.count_documents(
            {"schedule_token": reg["schedule_token"]}
        )
        assert cnt == 1, f"expected 1 history row, got {cnt}"

    def test_different_payload_same_nonce_rejected(self, seed_reg):
        reg = seed_reg()
        nonce = uuid.uuid4().hex
        r1 = requests.post(f"{API}/pub/availability/{reg['schedule_token']}",
                           json={"availability": True, "submission_nonce": nonce}, timeout=10)
        assert r1.status_code == 200
        r2 = requests.post(f"{API}/pub/availability/{reg['schedule_token']}",
                           json={"availability": False, "reason_id": "distance",
                                 "submission_nonce": nonce}, timeout=10)
        assert r2.status_code == 409

    def test_concurrent_duplicate_submissions(self, mdb, seed_reg):
        reg = seed_reg()
        nonce = uuid.uuid4().hex
        results = {}

        def _submit(idx):
            try:
                r = requests.post(f"{API}/pub/availability/{reg['schedule_token']}",
                                  json={"availability": True, "submission_nonce": nonce},
                                  timeout=10)
                results[idx] = r.status_code
            except Exception as e:  # pragma: no cover
                results[idx] = str(e)

        threads = [threading.Thread(target=_submit, args=(i,)) for i in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert all(v == 200 for v in results.values()), results
        cnt = mdb.bb_availability_responses.count_documents(
            {"schedule_token": reg["schedule_token"]}
        )
        assert cnt == 1, f"concurrent replay must not duplicate history, got {cnt}"


class TestIdempotencyWithdraw:
    def test_withdraw_nonce_replay(self, mdb, seed_reg):
        reg = seed_reg(chennai_availability_latest=False,
                       chennai_availability_reason_id="distance")
        nonce = uuid.uuid4().hex
        body = {"confirmed": True, "submission_nonce": nonce}
        r1 = requests.post(f"{API}/pub/availability/{reg['schedule_token']}/withdraw", json=body, timeout=10)
        r2 = requests.post(f"{API}/pub/availability/{reg['schedule_token']}/withdraw", json=body, timeout=10)
        assert r1.status_code == 200 and r2.status_code == 200
        assert r2.json().get("idempotent") is True
        cnt = mdb.bb_availability_responses.count_documents(
            {"schedule_token": reg["schedule_token"], "response_type": "withdrawal"}
        )
        assert cnt == 1


class TestIdempotencyReactivation:
    def test_reactivate_yes_nonce_replay(self, mdb, seed_reg):
        reg = seed_reg(withdrawn_by_candidate=True,
                       withdrawn_at=datetime.now(timezone.utc).isoformat(),
                       chennai_availability_latest=False, is_shortlisted=True)
        nonce = uuid.uuid4().hex
        body = {"choice": "yes", "submission_nonce": nonce}
        r1 = requests.post(f"{API}/pub/availability/{reg['schedule_token']}/reactivate", json=body, timeout=10)
        r2 = requests.post(f"{API}/pub/availability/{reg['schedule_token']}/reactivate", json=body, timeout=10)
        assert r1.status_code == 200 and r2.status_code == 200
        assert r2.json().get("idempotent") is True
        d = mdb.bb_registrations.find_one({"_id": reg["_id"]})
        # Reactivation count must only be +1 across replays.
        assert d["reactivation_count"] == 1


# =====================================================================
# TASK 4 — concurrent publish allocates monotonic versions
# =====================================================================
def test_concurrent_publish_monotonic_versions(admin_session):
    cur = admin_session.get(f"{API}/bb/availability-question/current", timeout=10).json()
    payload = dict(cur)
    original_question = cur.get("main_question")
    results = []
    def _publish(idx):
        body = dict(payload)
        body["main_question"] = f"Concurrent publish test {idx} — {uuid.uuid4().hex[:6]}"
        # Each thread must use its own Session to avoid cookie-jar locking.
        s = requests.Session()
        s.post(f"{API}/login", json={"username": "Admin User", "password": _admin_password()}, timeout=10)
        r = s.post(f"{API}/bb/availability-question/publish", json={"config": body}, timeout=10)
        results.append((r.status_code, r.json() if r.ok else r.text))

    threads = [threading.Thread(target=_publish, args=(i,)) for i in range(3)]
    for t in threads: t.start()
    for t in threads: t.join()

    versions = sorted({r[1].get("version") for r in results if isinstance(r[1], dict) and r[1].get("version")})
    assert len(versions) == 3, f"expected 3 distinct versions, got {versions}"
    assert versions == sorted(versions)

    # Restore the original question text so sibling tests don't observe a
    # test-created literal. Non-destructive: this is a brand-new PUBLISH on
    # top of the test ones, incrementing the version by one more.
    restore_body = dict(payload)
    restore_body["main_question"] = original_question
    admin_session.post(f"{API}/bb/availability-question/publish",
                       json={"config": restore_body}, timeout=10)


# =====================================================================
# TASK 1 — reminder worker filter semantics (unit-style)
# =====================================================================
def test_worker_filters_include_withdrawn_exclusion():
    """24h + missed workers MUST filter withdrawn_by_candidate; NO without
    withdrawal MUST stay eligible. This is a source-code assertion."""
    import pathlib
    src = pathlib.Path("/app/backend/bg_workers.py").read_text()
    # 24h reminder filter
    assert 'withdrawn_by_candidate": {"$ne": True}' in src
    # Missed-interview worker filter (same literal appears twice)
    assert src.count('withdrawn_by_candidate') >= 2
    # No NO-based exclusion was added (chennai_availability_latest must not
    # appear in bg_workers.py — spec says report-only).
    assert "chennai_availability_latest" not in src, (
        "bg_workers.py must not filter on availability — spec Task 10"
    )
