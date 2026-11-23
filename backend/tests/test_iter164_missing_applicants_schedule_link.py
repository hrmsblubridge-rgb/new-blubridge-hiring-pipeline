"""
iter164 — Missing Applicants export now includes an "Interview Scheduling Link"
column pulled from the existing per-candidate `bb_registrations.schedule_token`.
STRICT: read-only, no sends, no DB writes, no token generation.

Runs against the LIVE supervisor-managed backend (uses REACT_APP_BACKEND_URL)
to avoid TestClient's async-loop recycling noise. Seeds narrowly tagged rows,
cleans them up on teardown.

Run with:  pytest -xvs backend/tests/test_iter164_missing_applicants_schedule_link.py
"""
import os
import io
import csv
import uuid
import jwt
import pytest
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv("/app/backend/.env")

# Hit the running supervisor backend directly on localhost — same server the
# frontend talks to via ingress. Avoids TestClient lifespan / asyncio loop issues.
BASE_URL = os.environ.get("TEST_API_BASE", "http://127.0.0.1:8001")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
JWT_SECRET = os.environ["JWT_SECRET"]

TAG = uuid.uuid4().hex[:8]
TAG_EMAIL = lambda who: f"it164_{TAG}_{who}@example.test"  # noqa: E731


@pytest.fixture(scope="module", autouse=True)
def seed_and_cleanup():
    """Narrowly-scoped tagged seed rows. Teardown deletes ONLY rows carrying
    this test's uuid-suffixed email, so prod data is untouched."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]

    db.pipeline_data.insert_one({
        "name": f"IT164 WithLink {TAG}",
        "email": TAG_EMAIL("with"),
        "phone": "9443109903",
        "email_type": "shortlist",
        "schedule_date": "",
        "schedule_time": "",
        "last_update": today,
        "job_role": "AI Engineer",
    })
    db.bb_registrations.insert_one({
        "email": TAG_EMAIL("with"),
        "phone": "9443109903",
        "schedule_token": f"it164tok{TAG}",
        "last_update": today,
        "superseded": False,
    })
    db.pipeline_data.insert_one({
        "name": f"IT164 NoLink {TAG}",
        "email": TAG_EMAIL("nolink"),
        "phone": "9443100001",
        "email_type": "shortlist",
        "schedule_date": "",
        "schedule_time": "",
        "last_update": today,
        "job_role": "AI Engineer",
    })
    c.close()

    yield

    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    db.pipeline_data.delete_many({"email": {"$in": [TAG_EMAIL("with"), TAG_EMAIL("nolink")]}})
    db.bb_registrations.delete_many({"email": TAG_EMAIL("with")})
    c.close()


def _auth_headers() -> dict:
    """Craft a valid JWT directly for the seeded admin so the test is
    independent of whatever rotated password the live DB holds."""
    c = MongoClient(MONGO_URL)
    user = c[DB_NAME].bb_users.find_one({"role": "admin"}, {"_id": 0, "username": 1, "token_version": 1})
    c.close()
    assert user, "no admin user seeded in bb_users"
    tv = int(user.get("token_version") or 1)
    payload = {
        "sub": user["username"],
        "ver": tv,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _export(fmt: str):
    return requests.get(
        f"{BASE_URL}/api/bb/missing-applicants/export",
        params={"format": fmt, "report_type": "not_scheduled", "name": "IT164"},
        headers=_auth_headers(),
        timeout=30,
    )


def test_csv_export_includes_schedule_link_column_and_values():
    r = _export("csv")
    assert r.status_code == 200, r.text
    assert "text/csv" in r.headers["content-type"]

    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8"))))
    hdr = rows[0]
    assert hdr[-1] == "Interview Scheduling Link"
    assert "Name" in hdr and "Email" in hdr and "Result Status" in hdr

    link_idx = hdr.index("Interview Scheduling Link")
    email_idx = hdr.index("Email")
    by_email = {row[email_idx].strip().lower(): row[link_idx] for row in rows[1:]}

    assert TAG_EMAIL("with") in by_email, f"with-row missing: {list(by_email.keys())[:10]}"
    assert TAG_EMAIL("nolink") in by_email

    link_with = by_email[TAG_EMAIL("with")]
    assert link_with.startswith(("http://", "https://")), link_with
    assert f"/schedule-interview/it164tok{TAG}" in link_with

    assert by_email[TAG_EMAIL("nolink")] == "NOT AVAILABLE"


def test_xlsx_export_has_hyperlink_for_known_token():
    from openpyxl import load_workbook

    r = _export("xlsx")
    assert r.status_code == 200, r.text
    assert "spreadsheetml" in r.headers["content-type"]

    wb = load_workbook(io.BytesIO(r.content))
    ws = wb.active
    header_row = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    assert header_row[-1] == "Interview Scheduling Link"
    email_col_idx = header_row.index("Email")
    link_col = len(header_row)  # 1-indexed

    saw_link = saw_na = False
    for row in ws.iter_rows(min_row=2):
        email = (row[email_col_idx].value or "").strip().lower()
        if email == TAG_EMAIL("with"):
            cell = row[link_col - 1]
            assert cell.value and f"/schedule-interview/it164tok{TAG}" in str(cell.value)
            assert cell.hyperlink is not None, "with-token row must be a clickable hyperlink"
            saw_link = True
        elif email == TAG_EMAIL("nolink"):
            cell = row[link_col - 1]
            assert cell.value == "NOT AVAILABLE"
            assert cell.hyperlink is None, "NOT AVAILABLE must NOT be a hyperlink"
            saw_na = True
    assert saw_link and saw_na


def test_export_is_read_only_no_db_mutation():
    def _snapshot():
        c = MongoClient(MONGO_URL)
        db = c[DB_NAME]
        r = db.bb_registrations.find_one({"email": TAG_EMAIL("with")}, {"_id": 0})
        p = db.pipeline_data.find_one({"email": TAG_EMAIL("with")}, {"_id": 0})
        pn = db.pipeline_data.find_one({"email": TAG_EMAIL("nolink")}, {"_id": 0})
        rn = db.bb_registrations.find_one({"email": TAG_EMAIL("nolink")}, {"_id": 0})
        c.close()
        return r, p, pn, rn

    before = _snapshot()
    for _ in range(3):  # repeated exports stay deterministic
        assert _export("csv").status_code == 200
    after = _snapshot()
    assert before == after, "Export must be strictly read-only"
    assert after[3] is None, "No bb_registration must be auto-created by export"


def test_filters_and_existing_columns_preserved():
    """The new column is additive — all existing headers remain, in order,
    and the existing name-filter still narrows results."""
    r = _export("csv")
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8"))))
    hdr = rows[0]
    expected_prefix = [
        "Name", "Email", "Phone", "Current Location", "Job Role", "College",
        "College Type", "Degree", "Course", "Registered Date",
        "Scheduled Date", "Schedule Time", "Result Status",
    ]
    assert hdr[: len(expected_prefix)] == expected_prefix, hdr
    assert hdr[-1] == "Interview Scheduling Link"

    # Filter narrowed the file to our two tagged rows
    name_idx = hdr.index("Name")
    data_names = [row[name_idx] for row in rows[1:]]
    assert all("IT164" in n for n in data_names), data_names
