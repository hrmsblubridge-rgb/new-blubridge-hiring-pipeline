"""Enhancement 2 scope-at-publish integration tests.

End-to-end against a per-test scratch MongoDB database (same pattern as
`test_ai_calling_phase7ca.py` and `test_consent_publish_sync.py`):

  * Publishing a wording that explicitly mentions "Booking Confirmation"
    stamps `scope = [booking_confirmation, interview_confirmation,
    missed_interview_followup]` on the `ai_consent_versions` document.
  * Publishing a wording that does NOT mention it stamps the default
    two-workflow scope.
  * The scope on the version document is copied onto every
    `ai_candidate_consents` row created against that version, and into the
    `consent` history entry — so Booking Confirmation eligibility requires
    purpose-specific candidate consent, not just a "granted" status.
  * Historical consent records (older version_ids with no stored scope)
    fall back to `scope_for()` and are never retroactively upgraded.

SAFETY: scratch DB per test; never touches `hr_analytics` / preview /
production. Bolna and messaging are not invoked.
"""
import asyncio
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import AutoReconnect

from ai_calling import bolna_client as bc
from ai_calling import consent, store
from ai_calling.modes import derive_published_scope
from ai_calling.purposes import BOOKING, CONFIRM, MISSED, consent_covers
from ai_calling.store import SETTINGS_DEFAULTS

SCRATCH = "ai_scope_publish_pytest_scratch"
SCHED = "sched_" + "d" * 24


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reg():
    return {"full_name": "Scope Publish", "phone": "+919000000888", "email": "sp@example.invalid",
            "status": "Interview Scheduled", "is_shortlisted": True,
            "schedule_date": "2099-12-31", "schedule_time": "10:00:00",
            "otp_verified": False, "missed_marked": False, "superseded": False,
            "reschedule_count": 0, "isTest": False, "schedule_token": SCHED, "job_role": "Analyst"}


def run(case):
    async def main():
        client = AsyncIOMotorClient("mongodb://localhost:27017")
        name = f"{SCRATCH}_{uuid.uuid4().hex[:8]}"
        d = client[name]
        await d.ai_call_settings.insert_one({"_id": "global", **SETTINGS_DEFAULTS, "missed_enabled": False})
        await d.ai_candidate_consents.create_index("registration_id", unique=True)

        async def auth(_r):
            return "pytest-admin"

        async def boom(*_a, **_k):
            raise AssertionError("no provider request in scope-publish regression")

        prev_db = store._state.get("db")
        prev_auth = store._state.get("auth")
        prev_place = bc.place_call
        prev_get = bc._get
        store._state["db"] = d
        store._state["auth"] = auth
        bc.place_call = boom
        bc._get = boom
        try:
            await consent.ensure_consent_indexes()
            app = FastAPI()
            app.include_router(consent.public_router)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                         headers={"X-CSRF-Intent": "ai-calling"}) as tc:
                await case(tc, d)
        finally:
            # Per repo policy, app code never drops/clears real databases. The
            # scratch DB name is uniquely suffixed so it cannot collide with
            # preview (`hiring_ai_testing`) or production (`hr_analytics`).
            client.close()
            store._state["db"] = prev_db
            store._state["auth"] = prev_auth
            bc.place_call = prev_place
            bc._get = prev_get

    for attempt in range(3):
        try:
            asyncio.run(main())
            break
        except AutoReconnect:
            if attempt == 2:
                raise


async def _publish(d, title: str, text: str, scope: list = None):
    vid = f"consent-{_iso()[:10]}-{int(datetime.now(timezone.utc).timestamp() * 1_000_000)}"
    doc = {"version_id": vid, "title": title, "text": text,
           "published_by": "pytest-admin", "published_at": _iso()}
    if scope is not None:
        doc["scope"] = scope
    await d.ai_consent_versions.insert_one(doc)
    return vid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

APPROVED_TEXT_3_WORKFLOWS = (
    "I agree to receive automated AI voice calls from BluBridge Technologies "
    "about my scheduled in-person interview at the Chennai office. These may "
    "include: (a) a Booking Confirmation call shortly after I schedule my slot; "
    "(b) a Pre-Interview Confirmation call on the day before my interview; and "
    "(c) a Missed-Interview Follow-up call after the interview time."
)
TWO_WORKFLOW_TEXT = (
    "I agree to receive automated AI voice calls from BluBridge Technologies "
    "about my scheduled interview — a pre-interview confirmation call and, if "
    "my attendance has not been verified, a missed-interview follow-up call."
)


def test_get_returns_stored_scope_when_present():
    """`get_published_consent()` surfaces the scope stored on the version doc."""
    async def case(tc, d):
        vid = await _publish(d, title="Scope Stored", text=APPROVED_TEXT_3_WORKFLOWS,
                             scope=[BOOKING, CONFIRM, MISSED])
        pub = await consent.get_published_consent()
        assert pub["version"] == vid
        assert pub["scope"] == [BOOKING, CONFIRM, MISSED]
        # And the public endpoint reflects it (shape check only — scope is
        # intentionally NOT echoed on `/public/consent-text`; it is server-side only).
        body = (await tc.get("/public/consent-text")).json()
        assert body["version"] == vid
    run(case)


def test_get_derives_scope_from_text_when_version_doc_has_no_scope():
    """Rows written before the scope-at-publish fix lack a `scope` field —
    `get_published_consent()` must derive it from the text so the three-
    workflow phrase still gates `booking_confirmation`."""
    async def case(_tc, d):
        await _publish(d, title="Legacy no-scope", text=APPROVED_TEXT_3_WORKFLOWS, scope=None)
        pub = await consent.get_published_consent()
        assert pub["scope"] == [BOOKING, CONFIRM, MISSED]
        assert pub["scope"] == derive_published_scope(APPROVED_TEXT_3_WORKFLOWS)
    run(case)


def test_candidate_consent_stamps_stored_scope_and_blocks_booking_for_2wf_version():
    """Candidate submit records the EXACT scope the version grants:
    - two-workflow wording → `consent_scope = [CONFIRM, MISSED]`, booking blocked
    - three-workflow wording → `consent_scope = [BOOKING, CONFIRM, MISSED]`, booking allowed
    """
    async def case(tc, d):
        # ---- two-workflow publish
        vid_2wf = await _publish(d, title="Two WF", text=TWO_WORKFLOW_TEXT, scope=[CONFIRM, MISSED])
        rid = str((await d.bb_registrations.insert_one(_reg())).inserted_id)
        r = await tc.post(f"/public/booking-consent/{SCHED}",
                          json={"granted": True, "text_version": vid_2wf})
        assert r.status_code == 200, r.text
        c = await d.ai_candidate_consents.find_one({"registration_id": rid})
        assert c["consent_scope"] == [CONFIRM, MISSED]
        assert c["consent_text_version"] == vid_2wf
        covers = lambda purpose: consent_covers(  # noqa: E731
            {"status": c["consent_status"], "scope": c["consent_scope"], "text_version": c["consent_text_version"]},
            purpose)
        assert covers(CONFIRM) is True
        assert covers(MISSED) is True
        assert covers(BOOKING) is False  # <-- Booking Confirmation blocked by purpose-specific consent
        # history row must carry scope too
        hist = [h for h in (c.get("history") or []) if h.get("type") == "consent"]
        assert hist[-1]["scope"] == [CONFIRM, MISSED]
    run(case)


def test_candidate_consent_stamps_3workflow_scope_when_wording_describes_booking():
    """When the published wording describes all three workflows, candidate consent
    includes `booking_confirmation` and `consent_covers()` returns True for all three."""
    async def case(tc, d):
        vid = await _publish(d, title="3WF", text=APPROVED_TEXT_3_WORKFLOWS,
                             scope=[BOOKING, CONFIRM, MISSED])
        rid = str((await d.bb_registrations.insert_one(_reg())).inserted_id)
        r = await tc.post(f"/public/booking-consent/{SCHED}",
                          json={"granted": True, "text_version": vid})
        assert r.status_code == 200, r.text
        c = await d.ai_candidate_consents.find_one({"registration_id": rid})
        assert c["consent_scope"] == [BOOKING, CONFIRM, MISSED]
        assert c["consent_text_version"] == vid
        covers = lambda purpose: consent_covers(  # noqa: E731
            {"status": c["consent_status"], "scope": c["consent_scope"], "text_version": c["consent_text_version"]},
            purpose)
        assert covers(BOOKING) is True
        assert covers(CONFIRM) is True
        assert covers(MISSED) is True
    run(case)


def test_older_consent_record_not_upgraded_after_new_3wf_publish():
    """Candidate accepted V1 (two-workflow). Admin later publishes V2 with the
    three-workflow wording. The stored consent for V1 must remain two-workflow
    and must still block `booking_confirmation` — never retroactively upgraded."""
    async def case(tc, d):
        v1 = await _publish(d, title="V1 2WF", text=TWO_WORKFLOW_TEXT, scope=[CONFIRM, MISSED])
        rid = str((await d.bb_registrations.insert_one(_reg())).inserted_id)
        r = await tc.post(f"/public/booking-consent/{SCHED}",
                          json={"granted": True, "text_version": v1})
        assert r.status_code == 200
        # Admin publishes a NEW three-workflow version
        v2 = await _publish(d, title="V2 3WF", text=APPROVED_TEXT_3_WORKFLOWS,
                            scope=[BOOKING, CONFIRM, MISSED])
        pub = await consent.get_published_consent()
        assert pub["version"] == v2 and BOOKING in pub["scope"]
        # Stored candidate consent must still reference V1 and V1's scope.
        c = await d.ai_candidate_consents.find_one({"registration_id": rid})
        assert c["consent_text_version"] == v1
        assert c["consent_scope"] == [CONFIRM, MISSED]
        covers_booking = consent_covers(
            {"status": c["consent_status"], "scope": c["consent_scope"], "text_version": c["consent_text_version"]},
            BOOKING)
        assert covers_booking is False, "historical consent must not be retroactively upgraded"
    run(case)
