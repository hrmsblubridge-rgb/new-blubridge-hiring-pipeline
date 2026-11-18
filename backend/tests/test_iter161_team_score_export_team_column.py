"""iter161 — Team Score export regression.

Validates that the CSV + XLSX export restores the "Team" column at
Column A (iter161 fix). Confirms:
* Column A header = "Team"
* Every exported row carries the employee's actual team value
* Order of remaining base columns (Name, Email ID, …) is preserved
* INACTIVE separator row is still inserted
* Re-import of the exported XLSX round-trips without losing the team

SAFETY: scratch MongoDB database per test, operator-reaped by UUID prefix.
"""
import asyncio
import csv
import io
import uuid

import httpx
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient
from openpyxl import Workbook, load_workbook
from pymongo.errors import AutoReconnect

import team_score

SCRATCH = "ts_iter161_pytest_scratch"


def _wb() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Consolidated"
    ws.append([None] * 4)  # row 1: merged-banner placeholder
    ws.append(["Team", "Employment Status", "Name", "Role"])
    ws.append(["Research Unit", "Active", "Alpha", "Dev"])
    ws.append(["Research Unit", "Active", "Beta", "QA"])
    ws.append(["Business Team", "Inactive", "Gamma", "Lead"])
    ws.append(["Support Staffs", "Active", "Delta", "PM"])
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

        prev_db, prev_auth = team_score._db, team_score._require_auth
        team_score._db, team_score._require_auth = d, auth
        try:
            app = FastAPI()
            app.include_router(team_score.ts_router)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://t",
                headers={"X-CSRF-Intent": "ai-calling"},
            ) as tc:
                await tc.post(
                    "/api/team-score/import",
                    files={"file": ("x.xlsx", _wb(),
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                )
                await case(tc, d)
        finally:
            client.close()
            team_score._db, team_score._require_auth = prev_db, prev_auth

    for attempt in range(3):
        try:
            asyncio.run(main())
            break
        except AutoReconnect:
            if attempt == 2:
                raise


def test_csv_export_includes_team_column_at_position_0():
    async def case(tc, _d):
        r = await tc.get("/api/team-score/export", params={"fmt": "csv"})
        assert r.status_code == 200
        reader = list(csv.reader(io.StringIO(r.text)))
        header = reader[0]
        assert header[0] == "Team"
        assert header[1] == "Name"  # Name preserved at position 1
        # Every data row (minus the INACTIVE separator) must carry the team value.
        data_rows = [row for row in reader[1:] if row and row[0] != "INACTIVE EMPLOYEES"]
        teams = {r[1]: r[0] for r in data_rows}
        assert teams["Alpha"] == "Research Unit"
        assert teams["Beta"] == "Research Unit"
        assert teams["Gamma"] == "Business Team"
        assert teams["Delta"] == "Support Staffs"
    run(case)


def test_xlsx_export_includes_team_column_at_position_0():
    async def case(tc, _d):
        r = await tc.get("/api/team-score/export", params={"fmt": "xlsx"})
        assert r.status_code == 200
        wb = load_workbook(io.BytesIO(r.content))
        ws = wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        header = rows[0]
        assert header[0] == "Team"
        assert header[1] == "Name"
        teams = {r[1]: r[0] for r in rows[1:] if r and r[0] != "INACTIVE EMPLOYEES"}
        assert teams["Alpha"] == "Research Unit"
        assert teams["Delta"] == "Support Staffs"
        assert teams["Gamma"] == "Business Team"
    run(case)


def test_export_respects_status_filter():
    """With employee_status=active, the INACTIVE separator and Gamma are excluded."""
    async def case(tc, _d):
        r = await tc.get("/api/team-score/export",
                         params={"fmt": "csv", "employee_status": "active"})
        reader = list(csv.reader(io.StringIO(r.text)))
        names = [row[1] for row in reader[1:] if row and row[0] != "INACTIVE EMPLOYEES"]
        assert sorted(names) == ["Alpha", "Beta", "Delta"]  # Gamma excluded
        # No INACTIVE separator row.
        assert not any(row and row[0] == "INACTIVE EMPLOYEES" for row in reader[1:])
    run(case)


def test_reimport_of_exported_xlsx_preserves_team():
    """Round-trip: exported XLSX re-imported must preserve team values exactly."""
    async def case(tc, _d):
        exported = await tc.get("/api/team-score/export", params={"fmt": "xlsx"})
        # Re-import the exported workbook.
        r = await tc.post(
            "/api/team-score/import",
            files={"file": ("roundtrip.xlsx", exported.content,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert r.status_code == 200
        emps = (await tc.get("/api/team-score/employees")).json()["employees"]
        by_name = {e["name"]: e for e in emps}
        assert by_name["Alpha"]["team"] == "Research Unit"
        assert by_name["Beta"]["team"] == "Research Unit"
        assert by_name["Gamma"]["team"] == "Business Team"
        assert by_name["Delta"]["team"] == "Support Staffs"
    run(case)


def test_inactive_separator_row_width_matches_header():
    async def case(tc, _d):
        r = await tc.get("/api/team-score/export", params={"fmt": "csv"})
        reader = list(csv.reader(io.StringIO(r.text)))
        header_len = len(reader[0])
        sep = next(row for row in reader[1:] if row and row[0] == "INACTIVE EMPLOYEES")
        assert len(sep) == header_len
    run(case)
