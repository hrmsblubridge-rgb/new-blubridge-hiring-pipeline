"""Iter66 — Enhancement 2 Session 2 API contract sweep (scheduling booking/backup,
test-batch scenario, interview-reports AI columns, export, candidate-journey ai_calls_block)."""
import os
import io
from pathlib import Path

import pytest
import requests

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE}/api"
CSRF = {"X-CSRF-Intent": "ai-calling"}


def _pw():
    for line in Path("/app/backend/.env").read_text().splitlines():
        if line.startswith("TEST_ADMIN_PASSWORD="):
            return line.partition("=")[2].strip().strip('"')
    pytest.skip("no TEST_ADMIN_PASSWORD")


@pytest.fixture(scope="module")
def s():
    ses = requests.Session()
    r = ses.post(f"{API}/login", json={"username": "Admin User", "password": _pw()}, timeout=15)
    assert r.status_code == 200, r.text
    return ses


# --- Auth
def test_login_wrong_password_rejected():
    r = requests.post(f"{API}/login", json={"username": "Admin User", "password": "wrong"}, timeout=15)
    assert r.status_code in (400, 401, 403)


# --- Scheduling GET/PUT
class TestScheduling:
    def test_get_shape(self, s):
        r = s.get(f"{API}/ai-calling/scheduling", timeout=15)
        assert r.status_code == 200
        d = r.json()
        b = d["booking_confirmation"]
        for k in ("enabled", "target_hours", "retry_minutes", "window", "activated_at"):
            assert k in b
        assert b["target_range_hours"] == [1, 24]
        assert b["retry_range_minutes"] == [15, 240]
        bk = d["bolna_backup_agent"]
        assert bk["configured"] is False
        assert bk["agent_id_masked"] == ""
        assert bk["live"] is False
        assert isinstance(bk["note"], str) and bk["note"]

    @pytest.mark.parametrize("payload", [
        {"booking_target_hours": 0}, {"booking_target_hours": 25},
        {"booking_retry_minutes": 14}, {"booking_retry_minutes": 241},
        {"booking_target_hours": "3"}, {"booking_enabled": "yes"}, {"bogus_key": 1},
    ])
    def test_put_rejects_invalid(self, s, payload):
        r = s.put(f"{API}/ai-calling/scheduling", json=payload, headers=CSRF, timeout=15)
        assert r.status_code == 400, r.text

    def test_put_requires_csrf(self, s):
        r = s.put(f"{API}/ai-calling/scheduling", json={"booking_target_hours": 3}, timeout=15)
        assert r.status_code == 403

    def test_put_valid_and_activation_stamp_immutable(self, s):
        orig = s.get(f"{API}/ai-calling/scheduling", timeout=15).json()["booking_confirmation"]
        try:
            r = s.put(f"{API}/ai-calling/scheduling",
                      json={"booking_target_hours": 5, "booking_retry_minutes": 45}, headers=CSRF, timeout=30)
            assert r.status_code == 200, r.text
            b = r.json()["booking_confirmation"]
            assert b["target_hours"] == 5 and b["retry_minutes"] == 45
            g = s.get(f"{API}/ai-calling/scheduling", timeout=15).json()["booking_confirmation"]
            assert g["target_hours"] == 5 and g["retry_minutes"] == 45
            # flip ON → stamp
            r1 = s.put(f"{API}/ai-calling/scheduling", json={"booking_enabled": True}, headers=CSRF, timeout=30)
            assert r1.status_code == 200, r1.text
            st1 = r1.json()["booking_confirmation"]["activated_at"]
            assert st1
            if orig["activated_at"]:
                assert st1 == orig["activated_at"]
            s.put(f"{API}/ai-calling/scheduling", json={"booking_enabled": False}, headers=CSRF, timeout=30)
            r2 = s.put(f"{API}/ai-calling/scheduling", json={"booking_enabled": True}, headers=CSRF, timeout=30)
            assert r2.json()["booking_confirmation"]["activated_at"] == st1
        finally:
            s.put(f"{API}/ai-calling/scheduling",
                  json={"booking_enabled": bool(orig["enabled"]), "booking_target_hours": orig["target_hours"],
                        "booking_retry_minutes": orig["retry_minutes"]}, headers=CSRF, timeout=30)


# --- Test batches scenario selector
class TestBatches:
    def test_missing_purpose(self, s):
        r = s.post(f"{API}/ai-calling/test-batches", json={"name": "TEST_x", "candidate_ids": ["000000000000000000000000"]},
                   headers=CSRF, timeout=15)
        print("missing purpose status", r.status_code)
        assert r.status_code in (400, 422)

    def test_missing_purpose_is_400(self, s):
        r = s.post(f"{API}/ai-calling/test-batches", json={"name": "TEST_x", "candidate_ids": ["000000000000000000000000"]},
                   headers=CSRF, timeout=15)
        assert r.status_code == 400, f"spec says 400, got {r.status_code}"

    @pytest.mark.parametrize("p", ["", "bogus", "live_call"])
    def test_invalid_purpose(self, s, p):
        r = s.post(f"{API}/ai-calling/test-batches",
                   json={"name": "TEST_x", "candidate_ids": ["000000000000000000000000"], "call_purpose": p},
                   headers=CSRF, timeout=15)
        assert r.status_code == 400
        assert "call_purpose" in r.text

    @pytest.mark.parametrize("p", ["booking_confirmation", "interview_confirmation", "missed_interview_followup"])
    def test_valid_purpose_passes_purpose_check(self, s, p):
        # Non-consented fake id → must fail on consent check, NOT the purpose check
        r = s.post(f"{API}/ai-calling/test-batches",
                   json={"name": "TEST_x", "candidate_ids": ["000000000000000000000000"], "call_purpose": p},
                   headers=CSRF, timeout=15)
        assert r.status_code == 400
        assert "call_purpose" not in r.text
        assert "consented" in r.text.lower()


# --- Interview reports
class TestReports:
    def test_rows_have_ai_calls(self, s):
        r = s.get(f"{API}/bb/interview-reports", params={"limit": 50}, timeout=60)
        assert r.status_code == 200, r.text
        d = r.json()
        rows = d.get("rows") or d.get("data") or d.get("items") or []
        print("rows", len(rows), list(d.keys()))
        assert rows, "no rows to check"
        for row in rows:
            ac = row["ai_calls"]
            for k in ("booking_confirmation", "interview_confirmation", "missed_interview_followup", "reschedule_link"):
                assert isinstance(ac[k], dict)
            assert isinstance(row["ai_needs_hr_review"], bool)

    def test_export_fields_sections(self, s):
        r = s.get(f"{API}/bb/interview-reports/export-fields", timeout=60)
        assert r.status_code == 200
        secs = {x["id"]: x for x in r.json()["sections"]}
        assert set(secs) == {"registration", "interview", "ai_calling"}
        keys = {f["key"] for f in secs["ai_calling"]["fields"]}
        expected = {"ai_booking_outcome", "ai_booking_verified_at", "ai_booking_hr_flag", "ai_booking_reason",
                    "ai_confirm_outcome", "ai_confirm_verified_at", "ai_confirm_hr_flag", "ai_confirm_reason",
                    "ai_missed_outcome", "ai_missed_verified_at", "ai_missed_hr_flag", "ai_missed_reason",
                    "ai_reschedule_link_state", "ai_reschedule_link_source", "ai_reschedule_link_at"}
        assert expected <= keys

    @pytest.mark.parametrize("fmt", ["xlsx", "csv"])
    def test_export_ai_fields(self, s, fmt):
        r = s.get(f"{API}/bb/interview-reports/export",
                  params={"fields": "name,ai_booking_outcome,ai_confirm_outcome,ai_missed_outcome,ai_reschedule_link_state",
                          "format": fmt}, timeout=120)
        assert r.status_code in (200, 404), r.text[:300]
        if r.status_code == 200:
            assert len(r.content) > 50
            if fmt == "csv":
                head = r.content.decode("utf-8-sig").splitlines()[0]
                assert "AI Booking Call - Outcome" in head
                assert "AI Reschedule Link - State" in head
            else:
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(r.content))
                hdr = [c.value for c in next(wb.active.iter_rows(max_row=1))]
                assert "AI Booking Call - Outcome" in hdr

    def test_unauth_reports(self):
        r = requests.get(f"{API}/bb/interview-reports", timeout=15)
        assert r.status_code in (401, 403)


# --- Candidate journey
class TestJourney:
    def test_journey_ai_block(self, s):
        rows = s.get(f"{API}/bb/interview-reports", params={"limit": 10}, timeout=60).json()
        rows = rows.get("rows") or rows.get("data") or []
        checked = 0
        for row in rows[:5]:
            em = row.get("email")
            if not em or em == "-":
                continue
            r = s.get(f"{API}/bb/candidate-journey", params={"email": em}, timeout=60)
            assert r.status_code in (200, 404, 409), r.text[:200]
            if r.status_code == 200:
                d = r.json()
                assert "ai_calls_block" in d
                b = d["ai_calls_block"]
                if b is not None:
                    for k in ("booking_confirmation", "interview_confirmation", "missed_interview_followup",
                              "reschedule_link", "needs_hr_review"):
                        assert k in b
                    assert isinstance(b["needs_hr_review"], bool)
                checked += 1
        assert checked > 0

    def test_journey_requires_param(self, s):
        assert s.get(f"{API}/bb/candidate-journey", timeout=15).status_code == 400
