"""One-off: stage the Enhancement 2 three-workflow consent as a DRAFT only.

USAGE (from /app/backend):
    PYTHONPATH=/app/backend python scripts/stage_consent_draft_3workflows.py

SAFETY:
- Writes ONLY to `ai_consent_drafts._id=current` on the DB_NAME configured for
  the backend (hiring_ai_testing). Never writes to `ai_consent_versions`.
- Does not publish. Admin must explicitly publish from the AI Calling Settings
  editor UI.
- Prints the old draft content so the admin can verify what was replaced.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make /app/backend importable when called from anywhere
_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

TITLE = "Optional AI Voice Call Consent — Interview & Booking Updates"
TEXT = (
    "I agree to receive automated AI voice calls from BluBridge Technologies about my "
    "scheduled in-person interview at the Chennai office. These may include: "
    "(a) a Booking Confirmation call shortly after I schedule my slot to re-confirm the "
    "date/time and answer any immediate questions; "
    "(b) a Pre-Interview Confirmation call on the day before my interview; and "
    "(c) a Missed-Interview Follow-up call after the interview time if my attendance "
    "has not been verified at the office.\n\n"
    "I understand that these calls may be recorded and transcribed to confirm my "
    "booking and attendance, understand why I could not attend, process rescheduling "
    "requests, and record interview-related questions.\n\n"
    "I understand that I can withdraw my consent at any time through the provided "
    "consent-management option.\n\n"
    "Giving consent is optional and does not affect my ability to book or attend an "
    "interview."
)


async def main() -> None:
    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    if not mongo_url or not db_name:
        print("ERROR: MONGO_URL / DB_NAME not set in environment.", file=sys.stderr)
        sys.exit(2)
    if db_name == "hr_analytics":
        print("REFUSED: refusing to stage draft against hr_analytics.", file=sys.stderr)
        sys.exit(3)

    client = AsyncIOMotorClient(mongo_url)
    d = client[db_name]
    prior = await d.ai_consent_drafts.find_one({"_id": "current"})
    print(f"[stage-draft] DB={db_name}")
    if prior:
        print(f"[stage-draft] REPLACING draft previously saved by "
              f"{prior.get('saved_by', '?')} at {prior.get('saved_at', '?')}")
        print(f"              old title: {prior.get('title', '')[:80]!r}")
    else:
        print("[stage-draft] No existing draft; creating one.")

    now = datetime.now(timezone.utc).isoformat()
    await d.ai_consent_drafts.update_one(
        {"_id": "current"},
        {"$set": {"title": TITLE, "text": TEXT,
                  "saved_by": "enhancement-2-scope-staging", "saved_at": now}},
        upsert=True,
    )

    # Also log an audit row so the staging is traceable; no version is published.
    await d.ai_call_audit.insert_one({
        "action": "consent_draft_staged", "by": "enhancement-2-scope-staging",
        "at": now, "title": TITLE, "note": "3-workflow draft prepared; awaiting admin publish via UI",
    })

    staged = await d.ai_consent_drafts.find_one({"_id": "current"})
    print(f"[stage-draft] OK — draft now: title={staged['title']!r}")
    print(f"              text length: {len(staged['text'])} chars")
    print("[stage-draft] Next step: Admin → AI Calling Settings → "
          "Candidate AI Voice Call Consent → Publish")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
