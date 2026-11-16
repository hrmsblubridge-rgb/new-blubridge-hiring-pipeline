"""Regression: admin-published consent wording must reach the candidate
booking page (`GET /public/consent-text`) and gate the submit endpoint
(`POST /public/booking-consent/{token}`).

Uses the same scratch-DB pattern as `test_ai_calling_phase7ca.py`:
each test spins up a dedicated `ai_consent_sync_pytest_scratch_<uuid>` MongoDB
database and drops it at teardown. The preview / production databases
(`hiring_ai_testing`, `hr_analytics`) are never touched. Bolna and messaging
are monkeypatched so any provider request fails the test.
"""
import asyncio
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import AutoReconnect

from ai_calling import bolna_client as bc
from ai_calling import consent, modes, store
from ai_calling.store import SETTINGS_DEFAULTS

SCRATCH = "ai_consent_sync_pytest_scratch"
SCHED = "sched_" + "c" * 24


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reg():
    return {"full_name": "Consent Sync", "phone": "+919000000999", "email": "cs@example.invalid",
            "status": "Interview Scheduled", "is_shortlisted": True,
            "schedule_date": "2099-12-31", "schedule_time": "10:00:00",
            "otp_verified": False, "missed_marked": False, "superseded": False,
            "reschedule_count": 0, "isTest": False, "schedule_token": SCHED, "job_role": "Analyst"}


def run(case):
    calls = {"bolna": 0}

    async def main():
        client = AsyncIOMotorClient("mongodb://localhost:27017")
        name = f"{SCRATCH}_{uuid.uuid4().hex[:8]}"
        d = client[name]
        await d.ai_call_settings.insert_one({"_id": "global", **SETTINGS_DEFAULTS, "missed_enabled": False})
        await d.ai_candidate_consents.create_index("registration_id", unique=True)

        async def auth(_r):
            return "pytest-admin"

        async def boom(*_a, **_k):
            calls["bolna"] += 1
            raise AssertionError("no provider request in consent-sync regression")

        # Swap state in the module-level singleton used by both routers and helpers.
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
            # Scratch DB name is per-test UUID; it never collides with preview
            # (`hiring_ai_testing`) or production (`hr_analytics`). We do NOT
            # drop/delete here — per repo policy, app code never drops DBs or
            # issues unfiltered collection deletes. A separate operator cleanup
            # script reaps `ai_consent_sync_pytest_scratch_*` databases.
            client.close()
            store._state["db"] = prev_db
            store._state["auth"] = prev_auth
            bc.place_call = prev_place
            bc._get = prev_get

    for attempt in range(3):  # local mongod occasionally drops a fresh connection
        try:
            asyncio.run(main())
            break
        except AutoReconnect:
            if attempt == 2:
                raise
    assert calls["bolna"] == 0


async def _publish(d, title: str, text: str, version_id: str = None):
    vid = version_id or f"consent-{_iso()[:10]}-{int(datetime.now(timezone.utc).timestamp() * 1_000_000)}"
    await d.ai_consent_versions.insert_one({
        "version_id": vid, "title": title, "text": text,
        "published_by": "pytest-admin", "published_at": _iso(),
    })
    return vid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_get_consent_text_serves_latest_published():
    async def case(tc, d):
        vid = await _publish(d, title="Published Title",
                             text=("Published consent text paragraph one, long enough to clear the 60 character minimum "
                                   "enforced by the admin validator.\n\nParagraph two.\n\nParagraph three."))
        r = await tc.get("/public/consent-text")
        assert r.status_code == 200
        body = r.json()
        assert body["title"] == "Published Title"
        assert body["version"] == vid
        assert "Published consent text paragraph one" in body["text"]
        assert body["optional"] is True and body["default_checked"] is False
    run(case)


def test_prepublish_fallback_matches_modes_constants():
    """With no documents in `ai_consent_versions`, GET falls back to modes.CONSENT_TEXT_VERSION."""
    async def case(tc, _d):
        r = await tc.get("/public/consent-text")
        assert r.status_code == 200
        body = r.json()
        assert body["version"] == modes.CONSENT_TEXT_VERSION
        assert body["title"] == modes.CONSENT_TITLE
    run(case)


def test_draft_does_not_leak_to_candidate():
    """Writing to `ai_consent_drafts` must NEVER change `GET /public/consent-text`."""
    async def case(tc, d):
        pub_id = await _publish(d, title="Live Published", text="Live " * 20 + "\n\np2\n\np3")
        await d.ai_consent_drafts.update_one({"_id": "current"},
            {"$set": {"title": "DRAFT TITLE (never live)", "text": "Draft " * 30, "saved_by": "pytest", "saved_at": _iso()}},
            upsert=True)
        body = (await tc.get("/public/consent-text")).json()
        assert body["title"] == "Live Published"
        assert body["version"] == pub_id
    run(case)


def test_new_publish_is_served_on_next_candidate_load():
    """After publishing V2 the candidate GET flips from V1 to V2 immediately."""
    async def case(tc, d):
        v1 = await _publish(d, title="V1", text="V1 wording. " * 10)
        body1 = (await tc.get("/public/consent-text")).json()
        assert body1["version"] == v1 and body1["title"] == "V1"
        v2 = await _publish(d, title="V2", text="V2 wording. " * 10)
        body2 = (await tc.get("/public/consent-text")).json()
        assert body2["version"] == v2 and body2["title"] == "V2"
    run(case)


def test_stale_text_version_rejected_on_submit():
    """A candidate POST with a stale text_version must return 409 and NEVER create a consent row."""
    async def case(tc, d):
        v1 = await _publish(d, title="V1", text="V1 wording. " * 10)
        # Publish V2 so V1 is no longer the current version.
        await _publish(d, title="V2", text="V2 wording. " * 10)
        rid = str((await d.bb_registrations.insert_one(_reg())).inserted_id)
        r = await tc.post(f"/public/booking-consent/{SCHED}",
                          json={"granted": True, "text_version": v1})
        assert r.status_code == 409
        assert "Consent wording changed" in r.text
        assert (await d.ai_candidate_consents.find_one({"registration_id": rid})) is None
    run(case)


def test_current_text_version_accepted_and_stored():
    """Candidate submit with the currently-published version succeeds and the EXACT
    version_id is stamped on `ai_candidate_consents` (+ history)."""
    async def case(tc, d):
        vid = await _publish(d, title="Current", text="Current wording. " * 10)
        rid = str((await d.bb_registrations.insert_one(_reg())).inserted_id)
        r = await tc.post(f"/public/booking-consent/{SCHED}",
                          json={"granted": True, "text_version": vid})
        assert r.status_code == 200, r.text
        c = await d.ai_candidate_consents.find_one({"registration_id": rid})
        assert c["consent_status"] == "granted"
        assert c["consent_text_version"] == vid
        consent_hist = [h for h in (c.get("history") or []) if h.get("type") == "consent"]
        assert consent_hist and consent_hist[-1]["text_version"] == vid
    run(case)


def test_decline_records_not_given_at_current_version():
    """Declining consent stores `not_given` + the version the candidate saw — never AI-callable."""
    async def case(tc, d):
        vid = await _publish(d, title="Current", text="Current wording. " * 10)
        rid = str((await d.bb_registrations.insert_one(_reg())).inserted_id)
        r = await tc.post(f"/public/booking-consent/{SCHED}",
                          json={"granted": False, "text_version": vid})
        assert r.status_code == 200, r.text
        c = await d.ai_candidate_consents.find_one({"registration_id": rid})
        assert c["consent_status"] == "not_given"
        assert c["consent_text_version"] == vid
        assert c.get("consent_scope") in (None, [])
    run(case)


def test_previously_accepted_consent_is_not_upgraded():
    """Accept at V1 → publish V2 → stored consent must remain on V1; GET flips to V2."""
    async def case(tc, d):
        v1 = await _publish(d, title="V1", text="V1 wording. " * 10)
        rid = str((await d.bb_registrations.insert_one(_reg())).inserted_id)
        r = await tc.post(f"/public/booking-consent/{SCHED}",
                          json={"granted": True, "text_version": v1})
        assert r.status_code == 200, r.text
        v2 = await _publish(d, title="V2", text="V2 wording. " * 10)
        body = (await tc.get("/public/consent-text")).json()
        assert body["version"] == v2
        c = await d.ai_candidate_consents.find_one({"registration_id": rid})
        assert c["consent_text_version"] == v1, "historical consent must not be retroactively upgraded"
    run(case)
