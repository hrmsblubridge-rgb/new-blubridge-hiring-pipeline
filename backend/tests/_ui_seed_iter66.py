"""Seed/cleanup one TEST_ pipeline_data + bb_registrations pair with verified AI results (iter66 UI check)."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId
from pymongo import MongoClient

env = dict(l.split("=", 1) for l in Path("/app/backend/.env").read_text().splitlines() if "=" in l and not l.startswith("#"))
c = MongoClient(env["MONGO_URL"].strip('"'))[env["DB_NAME"].strip('"')]
assert env["DB_NAME"].strip('"') == "hiring_ai_testing"
STATE = Path("/tmp/iter66_seed.json")
EMAIL = "test_iter66_ai@test.local"

if sys.argv[1] == "seed":
    now = datetime.now(timezone.utc).isoformat()
    p = c.pipeline_data.insert_one({"name": "TEST_Iter66 AI", "email": EMAIL, "phone": "9000066066",
                                    "job_role": "Test", "_normalized_job_role": "Test", "schedule_date": "2026-11-16",
                                    "schedule_time": "11:00:00", "otp_verified": "", "_college_status": "NIRF",
                                    "source": "registration_form", "created_at": now})
    snap = lambda purpose, outcome, hr, reason: {"outcome": outcome, "verified_at": now, "purpose": purpose,
                                                 "booking_key": "k", "schedule_version": 1, "attempt": 1,
                                                 "concise_reason": reason, "needs_hr_review": hr}
    r = c.bb_registrations.insert_one({"form_id": "TEST_iter66", "full_name": "TEST_Iter66 AI", "email": EMAIL,
                                       "phone": "9000066066", "status": "Interview Scheduled", "superseded": False,
                                       "registered_at": now, "schedule_date": "2026-11-16",
                                       "ai_call_results": {
                                           "booking_confirmation": {"latest": snap("booking_confirmation", "CONFIRMED", False, "")},
                                           "missed_interview_followup": {"latest": snap("missed_interview_followup", "RESCHEDULE_REQUESTED", True, "Requested reschedule")}},
                                       "reschedule_link_last_state": "SENT_MOCKED",
                                       "reschedule_link_last_source": "missed_interview_followup",
                                       "reschedule_link_last_at": now})
    STATE.write_text(json.dumps({"p": str(p.inserted_id), "r": str(r.inserted_id)}))
    print("seeded", EMAIL)
else:
    st = json.loads(STATE.read_text())
    print(c.pipeline_data.delete_one({"_id": ObjectId(st["p"])}).deleted_count,
          c.bb_registrations.delete_one({"_id": ObjectId(st["r"])}).deleted_count)
