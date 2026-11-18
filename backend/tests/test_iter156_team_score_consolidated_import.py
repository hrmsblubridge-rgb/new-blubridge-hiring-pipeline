"""iter156 — Team Score consolidated-import regression.

Covers the Enhancement I wiring:
- Header auto-detection (skips merged group row on the Consolidated sheet).
- All 114 original columns preserved verbatim per employee (`raw_columns`).
- Base-field mapping incl. Team / Employment Status / Relieving Date / S.no.
- Blank vs 0 preserved (never converted to "Not Attempted" or 0).
- 2 unnamed rows routed to `ts_employees_unnamed` (admin review), never shown
  in the main table, and kept across reruns via soft-supersede.
- `/suggest` autocomplete returns name/email/team/status context.
- `/employees` filter accepts `team` and combines with other filters.
- `/filters` surfaces the Team dropdown values.
- `import_key` is stable across re-imports so 183 rows stay 183.

SAFETY: each test uses a per-run scratch MongoDB database with a UUID suffix
so production (`hr_analytics`) and preview (`hiring_ai_testing`) are never
touched. No deletes/drops from app code; only explicit test-fixture scratch
DBs are written to.
"""
import asyncio
import io
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient
from openpyxl import Workbook
from pymongo.errors import AutoReconnect

import team_score

SCRATCH = "ts_consolidated_pytest_scratch"


def _build_test_workbook() -> bytes:
    """Mini 6-column, 5-data-row consolidated workbook mirroring the real
    file: row 1 empty (merged group header), row 2 = actual column labels,
    then one unnamed row at the end for admin review."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Consolidated"
    ws.append([None] * 6)  # merged group header row
    ws.append(["Team", "Employment Status", "S.no", "Name", "Joining Date", "Mensa(40)"])
    ws.append(["Research Unit", "Active", 1, "Alpha One", "23/07/2025", 32])
    ws.append(["Research Unit", "Inactive", 2, "Beta Two", "01/01/2024", 0])  # zero score preserved
    ws.append(["Business Team", "Active", 3, "Gamma Three", "15/05/2025", None])  # blank preserved
    ws.append(["Support Staffs", "Inactive", 4, "Delta Four", "", 40])  # blank date
    ws.append(["Research Unit", "Inactive", None, None, None, None])  # UNNAMED row
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def run(case):
    async def main():
        client = AsyncIOMotorClient("mongodb://localhost:27017")
        name = f"{SCRATCH}_{uuid.uuid4().hex[:8]}"
        d = client[name]

        async def auth(_r):
            return "pytest-admin"

        prev_db = team_score._db
        prev_auth = team_score._require_auth
        team_score._db = d
        team_score._require_auth = auth
        try:
            app = FastAPI()
            app.include_router(team_score.ts_router)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://t") as tc:
                await case(tc, d)
        finally:
            # Scratch DB has a UUID suffix — operator script reaps leftover
            # `ts_consolidated_pytest_scratch_*` DBs. App code never drops.
            client.close()
            team_score._db = prev_db
            team_score._require_auth = prev_auth

    for attempt in range(3):
        try:
            asyncio.run(main())
            break
        except AutoReconnect:
            if attempt == 2:
                raise


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_preview_detects_header_row_and_counts():
    """Preview (`?preview=1`) returns the correct header row, counts by
    team/status, and lists the unnamed rows — without writing anything."""
    async def case(tc, d):
        xlsx = _build_test_workbook()
        r = await tc.post("/api/team-score/import?preview=1",
                          files={"file": ("mini.xlsx", xlsx,
                                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert r.status_code == 200
        body = r.json()
        assert body["preview"] is True
        assert body["header_row_index"] == 1
        assert body["header_count"] == 6
        c = body["counts"]
        assert c["named_rows"] == 4
        assert c["unnamed_rows"] == 1
        assert c["team_totals"] == {"Research Unit": 2, "Business Team": 1, "Support Staffs": 1}
        assert c["by_team_status"] == {
            "Research Unit|active": 1, "Research Unit|inactive": 1,
            "Business Team|active": 1, "Support Staffs|inactive": 1,
        }
        # Nothing persisted in preview mode.
        assert await d.ts_employees.count_documents({}) == 0
        assert await d.ts_employees_unnamed.count_documents({}) == 0
    run(case)


def test_commit_persists_all_rows_and_preserves_raw_columns():
    """Committing the same workbook inserts 4 named employees + 1 unnamed
    row; each employee carries all 6 original columns on `raw_columns`
    (blank stays blank, 0 stays 0)."""
    async def case(tc, d):
        xlsx = _build_test_workbook()
        r = await tc.post("/api/team-score/import",
                          files={"file": ("mini.xlsx", xlsx,
                                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        assert body["inserted"] == 4
        assert body["unnamed_rows_captured"] == 1
        assert await d.ts_employees.count_documents({}) == 4
        assert await d.ts_employees_unnamed.count_documents({"superseded_at": {"$exists": False}}) == 1

        # Beta Two's Mensa(40) = 0 must stay 0 (not blanked).
        beta = await d.ts_employees.find_one({"name": "Beta Two"})
        assert beta["round_scores"] == {"Mensa(40)": 0}
        assert len(beta["raw_columns"]) == 6
        # Gamma Three: Mensa(40) is blank — must NOT exist in round_scores.
        gamma = await d.ts_employees.find_one({"name": "Gamma Three"})
        assert "Mensa(40)" not in (gamma["round_scores"] or {})
        # And the raw_columns entry for the Mensa cell must be "" (blank preserved).
        mensa_cell = next(c for c in gamma["raw_columns"] if c["header"] == "Mensa(40)")
        assert mensa_cell["value"] == ""
        # Employment status is split: verbatim on `employment_status`, normalized on `employee_status`.
        assert gamma["employment_status"] == "Active"
        assert gamma["employee_status"] == "active"
        assert beta["employee_status"] == "inactive"
    run(case)


def test_reimport_is_idempotent():
    """Running the same import twice must leave exactly the same document
    count — no row multiplication."""
    async def case(tc, d):
        xlsx = _build_test_workbook()
        files = {"file": ("mini.xlsx", xlsx,
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        await tc.post("/api/team-score/import", files=files)
        # Re-POST the same bytes; must not duplicate.
        files2 = {"file": ("mini.xlsx", xlsx,
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        r2 = await tc.post("/api/team-score/import", files=files2)
        assert r2.status_code == 200
        assert await d.ts_employees.count_documents({}) == 4
        # Unnamed row is still exactly 1, not 2.
        active_unnamed = await d.ts_employees_unnamed.count_documents(
            {"superseded_at": {"$exists": False}})
        assert active_unnamed == 1
    run(case)


def test_filters_team_and_status_combine_correctly():
    async def case(tc, d):
        xlsx = _build_test_workbook()
        await tc.post("/api/team-score/import",
                      files={"file": ("mini.xlsx", xlsx,
                                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        # Research Unit + active → exactly Alpha One
        r = await tc.get("/api/team-score/employees?team=Research Unit&employee_status=active")
        assert r.status_code == 200
        data = r.json()
        assert data["filtered_count"] == 1
        assert data["employees"][0]["name"] == "Alpha One"
        assert data["total_count"] == 4
        # Team filter alone
        r2 = await tc.get("/api/team-score/employees?team=Research Unit")
        assert r2.json()["filtered_count"] == 2
        # Status alone
        r3 = await tc.get("/api/team-score/employees?employee_status=inactive")
        assert r3.json()["filtered_count"] == 2
        # /filters exposes team dropdown options
        f = await tc.get("/api/team-score/filters")
        assert "team" in f.json()
        assert sorted(f.json()["team"]) == ["Business Team", "Research Unit", "Support Staffs"]
    run(case)


def test_suggest_matches_partial_name_and_returns_context():
    """Autocomplete tolerates case, partial match, and returns name/team/
    status context the dropdown needs."""
    async def case(tc, d):
        xlsx = _build_test_workbook()
        await tc.post("/api/team-score/import",
                      files={"file": ("mini.xlsx", xlsx,
                                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        r = await tc.get("/api/team-score/suggest?q=alp")
        s = r.json()["suggestions"]
        assert len(s) == 1
        assert s[0]["name"] == "Alpha One"
        assert s[0]["team"] == "Research Unit"
        assert s[0]["employee_status"] == "active"
        # Empty query → no suggestions (prevents accidental full dump).
        assert (await tc.get("/api/team-score/suggest?q=")).json()["suggestions"] == []
    run(case)


def test_unnamed_endpoint_surfaces_review_rows_with_all_columns():
    """`/unnamed` returns the 1 unnamed row, with all 6 columns preserved
    on `raw_columns`, so admins can reconcile without touching the DB."""
    async def case(tc, d):
        xlsx = _build_test_workbook()
        await tc.post("/api/team-score/import",
                      files={"file": ("mini.xlsx", xlsx,
                                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        r = await tc.get("/api/team-score/unnamed")
        rows = r.json()["unnamed"]
        assert len(rows) == 1
        row = rows[0]
        assert row["team"] == "Research Unit"
        assert row["employee_status"] == "inactive"
        assert len(row["raw_columns"]) == 6
        # The name cell must be stored as empty, never fabricated.
        name_cell = next(c for c in row["raw_columns"] if c["header"] == "Name")
        assert name_cell["value"] == ""
