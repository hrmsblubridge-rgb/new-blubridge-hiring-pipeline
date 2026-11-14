"""Seed/cleanup TEST_ regs for iter65 UI testing. Usage: python _ui_seed.py seed|cleanup"""
import sys, json, uuid
from datetime import datetime, timezone
from pathlib import Path
from pymongo import MongoClient

env = {l.split('=', 1)[0]: l.split('=', 1)[1].strip().strip('"') for l in Path('/app/backend/.env').read_text().splitlines() if '=' in l}
assert env['DB_NAME'] == 'hiring_ai_testing'
db = MongoClient(env['MONGO_URL'])[env['DB_NAME']]
F = Path('/tmp/iter65_ui_tokens.json')

if sys.argv[1] == 'seed':
    toks = {}
    for k, extra in (("fresh", {}), ("fresh2", {}), ("withdrawn", {"withdrawn_by_candidate": True, "chennai_availability_latest": False})):
        t = f"iter65-ui-{uuid.uuid4().hex[:12]}"
        db.bb_registrations.insert_one({"form_id": "iter65-ui-form", "full_name": f"TEST_iter65_ui_{k}",
            "email": f"{t}@test.local", "phone": "0000" + uuid.uuid4().int.__str__()[:6],
            "status": "Interview Not Scheduled", "is_shortlisted": True, "schedule_token": t,
            "superseded": False, "registered_at": datetime.now(timezone.utc).isoformat(), **extra})
        toks[k] = t
    F.write_text(json.dumps(toks)); print(json.dumps(toks))
elif sys.argv[1] == 'count':
    for k, t in json.loads(F.read_text()).items():
        rows = list(db.bb_availability_responses.find({"schedule_token": t}, {"_id": 0, "response_type": 1, "submission_nonce": 1, "availability": 1}))
        print(k, len(rows), rows)
else:
    toks = list(json.loads(F.read_text()).values())
    print(db.bb_registrations.delete_many({"schedule_token": {"$in": toks}}).deleted_count,
          db.bb_availability_responses.delete_many({"schedule_token": {"$in": toks}}).deleted_count)
