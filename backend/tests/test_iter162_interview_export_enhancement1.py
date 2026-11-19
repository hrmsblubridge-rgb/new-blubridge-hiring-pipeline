"""iter162 — Interview Schedule export Enhancement 1 regression suite.

Confirms the 12 newly-added "Candidate Responses" columns on
`GET /api/bb/interview-reports/export`:
  * Chennai in-person availability (YES / NO / NOT ANSWERED)
  * Chennai availability reason / other reason (only when NO)
  * Chennai availability answered-at (IST)
  * AI Voice Call Consent (YES / NO / NOT ANSWERED)
  * AI Consent Version (exact version accepted, not the currently-published one)
  * AI Consent Submitted At (IST)
  * Application Withdrawal (YES / NO / NOT ANSWERED)
  * Withdrawal reason + Withdrawal responded-at (IST)
  * Current Application Status
  * HR Review Required (YES / NO)

Guarantees preserved:
  * Enhancement 2 AI-calling columns still render.
  * De-dupe by (email, schedule_date) still applies.
  * CSV formula-injection is neutralised for all free-text fields.
  * Empty dataset still returns 404.
  * The sections endpoint lists the new "candidate_responses" section
    unconditionally so admins can request them even on empty call history.

SAFETY: scratch MongoDB database per test, operator-reaped by UUID prefix.
No preview / production data is touched.
"""
import asyncio
import csv
import gzip
import io
import uuid

import httpx
from bson import ObjectId
from fastapi import FastAPI, Request
from motor.motor_asyncio import AsyncIOMotorClient
from openpyxl import load_workbook
from pymongo.errors import AutoReconnect

import bb_modules

SCRATCH = "ts_iter162_pytest_scratch"


def _seed():
    """Return (pipeline_data, bb_registrations, ai_candidate_consents)."""
    rid_available = ObjectId()
    rid_no_reason = ObjectId()
    rid_no_other = ObjectId()
    rid_legacy = ObjectId()
    rid_withdrawn = ObjectId()
    rid_keep_active = ObjectId()
    rid_consent_old = ObjectId()
    rid_consent_refused = ObjectId()
    pd = [
        # Chennai YES, consent granted on current version.
        {"name": "Aarav Yes", "email": "aarav@example.com", "phone": "9999900001",
         "schedule_date": "2026-03-10", "schedule_time": "10:00", "otp_verified": True,
         "_college_status": "Top", "_normalized_job_role": "SDE", "job_role": "SDE"},
        # Chennai NO with a selected reason (no `Other` text).
        {"name": "Bodhi NoReason", "email": "bodhi@example.com", "phone": "9999900002",
         "schedule_date": "2026-03-11", "schedule_time": "11:00", "otp_verified": False,
         "_normalized_job_role": "SDE", "job_role": "SDE"},
        # Chennai NO with `Other` text containing a formula-injection payload.
        {"name": "Charu Other", "email": "charu@example.com", "phone": "9999900003",
         "schedule_date": "2026-03-12", "schedule_time": "12:00", "otp_verified": False,
         "_normalized_job_role": "SDE", "job_role": "SDE"},
        # Legacy — never asked about Chennai availability or consent.
        {"name": "Devi Legacy", "email": "devi@example.com", "phone": "9999900004",
         "schedule_date": "2026-03-13", "schedule_time": "09:30", "otp_verified": False,
         "_normalized_job_role": "SDE", "job_role": "SDE"},
        # Candidate that actually withdrew.
        {"name": "Esha Withdrawn", "email": "esha@example.com", "phone": "9999900005",
         "schedule_date": "2026-03-14", "schedule_time": "15:00", "otp_verified": False,
         "_normalized_job_role": "SDE", "job_role": "SDE"},
        # Candidate who answered NO to Chennai but explicitly kept the application.
        {"name": "Farah Keep", "email": "farah@example.com", "phone": "9999900006",
         "schedule_date": "2026-03-15", "schedule_time": "15:30", "otp_verified": False,
         "_normalized_job_role": "SDE", "job_role": "SDE"},
        # Consent granted on an OLDER published version (version pinning test).
        {"name": "Gauri Oldversion", "email": "gauri@example.com", "phone": "9999900007",
         "schedule_date": "2026-03-16", "schedule_time": "10:00", "otp_verified": False,
         "_normalized_job_role": "SDE", "job_role": "SDE"},
        # Consent explicitly withdrawn (refused).
        {"name": "Hari Refused", "email": "hari@example.com", "phone": "9999900008",
         "schedule_date": "2026-03-17", "schedule_time": "10:00", "otp_verified": False,
         "_normalized_job_role": "SDE", "job_role": "SDE"},
    ]
    regs = [
        # Aarav — Chennai YES, status fine.
        {"_id": rid_available, "email": "aarav@example.com", "schedule_date": "2026-03-10",
         "chennai_availability_latest": True,
         "chennai_availability_latest_at": "2026-02-01T05:00:00+00:00",
         "chennai_availability_withdraw_decision": False},
        # Bodhi — Chennai NO with a reason label.
        {"_id": rid_no_reason, "email": "bodhi@example.com", "schedule_date": "2026-03-11",
         "chennai_availability_latest": False,
         "chennai_availability_latest_at": "2026-02-02T06:45:00+00:00",
         "chennai_availability_reason_id": "distance",
         "chennai_availability_reason_text": "The distance is too far for me.",
         "chennai_availability_withdraw_decision": False},
        # Charu — Chennai NO with an `Other` payload that LOOKS like a formula.
        {"_id": rid_no_other, "email": "charu@example.com", "schedule_date": "2026-03-12",
         "chennai_availability_latest": False,
         "chennai_availability_latest_at": "2026-02-03T07:15:00+00:00",
         "chennai_availability_reason_id": "other",
         "chennai_availability_reason_text": "Other",
         "chennai_availability_other_text": "=HYPERLINK(\"http://evil\",\"hi\")",
         "chennai_availability_withdraw_decision": False},
        # Devi — legacy, no availability answer, no consent.
        {"_id": rid_legacy, "email": "devi@example.com", "schedule_date": "2026-03-13"},
        # Esha — withdrawn.
        {"_id": rid_withdrawn, "email": "esha@example.com", "schedule_date": "2026-03-14",
         "chennai_availability_latest": False,
         "chennai_availability_latest_at": "2026-02-04T07:00:00+00:00",
         "chennai_availability_reason_id": "not_interested",
         "chennai_availability_reason_text": "Not interested at this time.",
         "chennai_availability_withdraw_decision": True,
         "withdrawn_by_candidate": True,
         "withdrawn_at": "2026-02-04T07:05:00+00:00",
         "withdrawal_reason_id": "not_interested",
         "withdrawal_reason_text": "Not interested at this time.",
         "needs_hr_review": True},
        # Farah — answered NO but chose to KEEP the application active.
        {"_id": rid_keep_active, "email": "farah@example.com", "schedule_date": "2026-03-15",
         "chennai_availability_latest": False,
         "chennai_availability_latest_at": "2026-02-05T07:00:00+00:00",
         "chennai_availability_reason_id": "virtual_preferred",
         "chennai_availability_reason_text": "Prefers virtual interview.",
         "chennai_availability_withdraw_decision": False},
        # Gauri — consented on an older version.
        {"_id": rid_consent_old, "email": "gauri@example.com", "schedule_date": "2026-03-16"},
        # Hari — explicitly refused consent.
        {"_id": rid_consent_refused, "email": "hari@example.com", "schedule_date": "2026-03-17"},
    ]
    consents = [
        # Aarav — granted on the current version v3.
        {"registration_id": str(rid_available), "consent_status": "granted",
         "consent_text_version": "v3",
         "consent_timestamp": "2026-02-01T05:05:00+00:00",
         "updated_at": "2026-02-01T05:05:00+00:00",
         "last_change_at": "2026-02-01T05:05:00+00:00"},
        # Gauri — granted on OLD version v1 (version pinning).
        {"registration_id": str(rid_consent_old), "consent_status": "granted",
         "consent_text_version": "v1",
         "consent_timestamp": "2025-12-10T04:00:00+00:00",
         "updated_at": "2025-12-10T04:00:00+00:00",
         "last_change_at": "2025-12-10T04:00:00+00:00"},
        # Hari — withdrawn (explicit refusal) on v2.
        {"registration_id": str(rid_consent_refused), "consent_status": "withdrawn",
         "consent_text_version": "v2",
         "consent_withdrawn_at": "2026-01-20T09:30:00+00:00",
         "updated_at": "2026-01-20T09:30:00+00:00",
         "last_change_at": "2026-01-20T09:30:00+00:00"},
        # Charu — explicit `not_given` record (never submitted, but a doc exists).
        {"registration_id": str(rid_no_other), "consent_status": "not_given",
         "consent_text_version": None,
         "updated_at": "2026-01-10T08:00:00+00:00",
         "last_change_at": "2026-01-10T08:00:00+00:00"},
    ]
    return pd, regs, consents


def run(case):
    async def main():
        client = AsyncIOMotorClient("mongodb://localhost:27017")
        name = f"{SCRATCH}_{uuid.uuid4().hex[:8]}"
        d = client[name]

        async def auth(_r):
            return "pytest-admin"

        prev_db, prev_auth = bb_modules._db, bb_modules._auth_fn
        bb_modules._db, bb_modules._auth_fn = d, auth
        try:
            pd, regs, consents = _seed()
            if pd:
                await d.pipeline_data.insert_many(pd)
            if regs:
                await d.bb_registrations.insert_many(regs)
            if consents:
                await d.ai_candidate_consents.insert_many(consents)

            app = FastAPI()
            app.include_router(bb_modules.bb_router)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://t",
                headers={"X-CSRF-Intent": "ai-calling"},
            ) as tc:
                await case(tc, d)
        finally:
            client.close()
            bb_modules._db, bb_modules._auth_fn = prev_db, prev_auth

    for attempt in range(3):
        try:
            asyncio.run(main())
            break
        except AutoReconnect:
            if attempt == 2:
                raise


# ─── helpers ──────────────────────────────────────────────────────────────

ALL_E1_FIELDS = (
    "name,email,schedule_date,"
    "ch_in_person_availability,ch_availability_reason,ch_availability_other,"
    "ch_availability_at,consent_ai_voice,consent_version,consent_submitted_at,"
    "wd_application,wd_reason,wd_responded_at,app_status,hr_review_required"
)


async def _csv_rows(tc, fields=ALL_E1_FIELDS):
    r = await tc.get("/api/bb/interview-reports/export",
                     params={"format": "csv", "fields": fields})
    assert r.status_code == 200, r.text
    # httpx auto-decompresses responses with `Content-Encoding: gzip`, so
    # r.text is already the plain CSV body — no manual gunzip needed.
    body = r.text
    rows = list(csv.reader(io.StringIO(body)))
    headers = rows[0]
    out = [dict(zip(headers, row)) for row in rows[1:]]
    return headers, out


def _by_email(rows):
    """CSV header labels are capitalised ("Email"), not the field keys."""
    return {r["Email"]: r for r in rows}


# ─── Catalog / sections endpoint ──────────────────────────────────────────

def test_sections_endpoint_lists_candidate_responses_section():
    async def case(tc, _d):
        r = await tc.get("/api/bb/interview-reports/export-fields")
        assert r.status_code == 200
        body = r.json()
        sections = {s["id"]: s for s in body["sections"]}
        assert "candidate_responses" in sections
        cr = sections["candidate_responses"]
        assert cr["label"] == "Candidate Responses (Enhancement 1)"
        keys = {f["key"] for f in cr["fields"]}
        assert keys == {
            "ch_in_person_availability", "ch_availability_reason",
            "ch_availability_other", "ch_availability_at",
            "consent_ai_voice", "consent_version", "consent_submitted_at",
            "wd_application", "wd_reason", "wd_responded_at",
            "app_status", "hr_review_required",
        }
        # Enhancement 2 AI Calling section remains intact.
        assert "ai_calling" in sections
    run(case)


# ─── Chennai availability — YES / NO / NO+other / legacy ─────────────────

def test_chennai_availability_yes_no_not_answered():
    async def case(tc, _d):
        _, rows = await _csv_rows(tc)
        by_email = _by_email(rows)

        # Aarav — explicit YES, reason columns must be blank even if present.
        assert by_email["aarav@example.com"]["Chennai In-Person Availability"] == "YES"
        assert by_email["aarav@example.com"]["Chennai Availability Reason"] == ""
        assert by_email["aarav@example.com"]["Chennai Availability Other Reason"] == ""

        # Bodhi — NO + reason label, no `Other` text.
        assert by_email["bodhi@example.com"]["Chennai In-Person Availability"] == "NO"
        assert by_email["bodhi@example.com"]["Chennai Availability Reason"] \
            == "The distance is too far for me."
        assert by_email["bodhi@example.com"]["Chennai Availability Other Reason"] == ""

        # Charu — NO + `Other` payload neutralised (leading = became '=).
        assert by_email["charu@example.com"]["Chennai In-Person Availability"] == "NO"
        other = by_email["charu@example.com"]["Chennai Availability Other Reason"]
        assert other.startswith("'="), f"formula injection not neutralised: {other!r}"
        assert "evil" in other

        # Devi — legacy, never answered.
        assert by_email["devi@example.com"]["Chennai In-Person Availability"] == "NOT ANSWERED"
        assert by_email["devi@example.com"]["Chennai Availability Reason"] == ""
        assert by_email["devi@example.com"]["Chennai Availability Other Reason"] == ""
    run(case)


def test_chennai_answered_at_is_ist():
    async def case(tc, _d):
        _, rows = await _csv_rows(tc)
        by_email = _by_email(rows)
        # Bodhi: UTC 2026-02-02 06:45 → IST 2026-02-02 12:15.
        assert by_email["bodhi@example.com"]["Chennai Availability Answered At (IST)"] \
            == "2026-02-02 12:15 IST"
        assert by_email["devi@example.com"]["Chennai Availability Answered At (IST)"] == ""
    run(case)


# ─── AI Voice Call Consent (version pinning + decision states) ────────────

def test_consent_states():
    async def case(tc, _d):
        _, rows = await _csv_rows(tc)
        by_email = _by_email(rows)

        # Aarav — granted on v3 (current).
        assert by_email["aarav@example.com"]["AI Voice Call Consent"] == "YES"
        assert by_email["aarav@example.com"]["AI Consent Version"] == "v3"
        assert by_email["aarav@example.com"]["AI Consent Submitted At (IST)"] \
            == "2026-02-01 10:35 IST"

        # Gauri — granted on OLDER v1; export must pin to v1, NOT the latest.
        assert by_email["gauri@example.com"]["AI Voice Call Consent"] == "YES"
        assert by_email["gauri@example.com"]["AI Consent Version"] == "v1"

        # Hari — explicit refusal on v2.
        assert by_email["hari@example.com"]["AI Voice Call Consent"] == "NO"
        assert by_email["hari@example.com"]["AI Consent Version"] == "v2"
        assert by_email["hari@example.com"]["AI Consent Submitted At (IST)"] \
            == "2026-01-20 15:00 IST"

        # Charu — explicit `not_given` consent doc → NOT ANSWERED, no version.
        assert by_email["charu@example.com"]["AI Voice Call Consent"] == "NOT ANSWERED"
        assert by_email["charu@example.com"]["AI Consent Version"] == ""
        assert by_email["charu@example.com"]["AI Consent Submitted At (IST)"] == ""

        # Devi — no consent doc at all → NOT ANSWERED, blank version.
        assert by_email["devi@example.com"]["AI Voice Call Consent"] == "NOT ANSWERED"
        assert by_email["devi@example.com"]["AI Consent Version"] == ""

        # Booking-only candidate (Bodhi) with no consent doc → NOT ANSWERED.
        # (Booking by itself does NOT imply YES.)
        assert by_email["bodhi@example.com"]["AI Voice Call Consent"] == "NOT ANSWERED"
    run(case)


# ─── Withdrawal distinction ───────────────────────────────────────────────

def test_withdrawal_yes_no_not_answered():
    async def case(tc, _d):
        _, rows = await _csv_rows(tc)
        by_email = _by_email(rows)

        # Esha — actually withdrew.
        assert by_email["esha@example.com"]["Application Withdrawal"] == "YES"
        assert by_email["esha@example.com"]["Withdrawal Reason"] == "Not interested at this time."
        assert by_email["esha@example.com"]["Withdrawal Responded At (IST)"] \
            == "2026-02-04 12:35 IST"
        assert by_email["esha@example.com"]["Current Application Status"] == "Withdrawn"
        assert by_email["esha@example.com"]["HR Review Required"] == "YES"

        # Farah — answered NO to Chennai but explicitly KEPT application active.
        assert by_email["farah@example.com"]["Application Withdrawal"] == "NO"
        assert by_email["farah@example.com"]["Withdrawal Reason"] == ""
        assert by_email["farah@example.com"]["Withdrawal Responded At (IST)"] == ""
        assert by_email["farah@example.com"]["HR Review Required"] == "NO"

        # Devi — never asked about withdrawal.
        assert by_email["devi@example.com"]["Application Withdrawal"] == "NOT ANSWERED"
    run(case)


# ─── Current Application Status derivation ────────────────────────────────

def test_current_application_status_derivation():
    async def case(tc, _d):
        _, rows = await _csv_rows(tc)
        by_email = _by_email(rows)
        # Aarav — attended (otp_verified=True).
        assert by_email["aarav@example.com"]["Current Application Status"] == "Attended"
        # Bodhi — scheduled (has schedule_date, not attended).
        assert by_email["bodhi@example.com"]["Current Application Status"] == "Interview Scheduled"
        # Esha — withdrawn wins over any other status.
        assert by_email["esha@example.com"]["Current Application Status"] == "Withdrawn"
    run(case)


# ─── XLSX round-trip + formula-injection neutralisation ───────────────────

def test_xlsx_export_matches_csv_and_blocks_formula_injection():
    async def case(tc, _d):
        r = await tc.get("/api/bb/interview-reports/export",
                         params={"format": "xlsx", "fields": ALL_E1_FIELDS})
        assert r.status_code == 200
        wb = load_workbook(io.BytesIO(r.content))
        ws = wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        headers = rows[0]
        docs = [dict(zip(headers, row)) for row in rows[1:]]
        by_email = {d["Email"]: d for d in docs}
        # Charu's `Other` cell: openpyxl must NOT return it as a numeric
        # formula result — the neutralising "'" prefix makes it a string.
        other = by_email["charu@example.com"]["Chennai Availability Other Reason"]
        assert other.startswith("'="), f"xlsx cell not neutralised: {other!r}"
        # Column labels present at Enhancement 1 positions.
        assert "Chennai In-Person Availability" in headers
        assert "AI Voice Call Consent" in headers
        assert "Application Withdrawal" in headers
        assert "Current Application Status" in headers
        assert "HR Review Required" in headers
    run(case)


# ─── Preservation guarantees ──────────────────────────────────────────────

def test_default_export_keeps_enhancement2_ai_columns():
    """Omitting `fields` → full catalog. All three AI purposes + reschedule
    link columns must still appear alongside the new Enhancement 1 columns."""
    async def case(tc, _d):
        r = await tc.get("/api/bb/interview-reports/export",
                         params={"format": "csv"})
        assert r.status_code == 200
        body = r.text  # httpx auto-decompresses gzip
        headers = next(csv.reader(io.StringIO(body)))
        for h in ("AI Booking Call - Outcome",
                  "AI Interview Confirmation - Outcome",
                  "AI Missed Follow-up - Outcome",
                  "AI Reschedule Link - State",
                  "Chennai In-Person Availability",
                  "AI Voice Call Consent",
                  "Application Withdrawal"):
            assert h in headers, f"missing column {h!r}"
    run(case)


def test_dedup_by_email_schedule_date_preserved():
    """A duplicate pipeline row with the same (email, schedule_date) must
    collapse to a single export row."""
    async def case(tc, d):
        await d.pipeline_data.insert_one({
            "name": "Aarav Yes", "email": "aarav@example.com",
            "schedule_date": "2026-03-10", "schedule_time": "10:00",
            "otp_verified": True, "_normalized_job_role": "SDE",
        })
        _, rows = await _csv_rows(tc)
        emails = [r["Email"] for r in rows]
        assert emails.count("aarav@example.com") == 1
    run(case)


def test_empty_dataset_returns_404():
    async def case(tc, d):
        await d.pipeline_data.delete_many({})
        r = await tc.get("/api/bb/interview-reports/export",
                         params={"format": "csv", "fields": ALL_E1_FIELDS})
        assert r.status_code == 404
    run(case)
