"""iter160 — Team Score Admin Import Review delete + email merge + sort integrity.

Guarantees the NEW endpoints introduced for Admin Import Review deletion:
* DELETE /api/team-score/unnamed/{id}   — removes a single ts_employees_unnamed row
* POST   /api/team-score/unnamed/bulk-delete — removes only the listed `_id` set

Plus end-to-end backend-side guarantees required by iter160:
* Email columns H ("Email ID/ Teams") and I ("Official Email/ Teams ID") are
  BOTH preserved verbatim in raw_columns on every imported doc (no destructive
  merge at the DB layer). The frontend does the display-only consolidation.
* The ts_employees bulk-delete endpoint touches ts_employees ONLY — Admin
  Import Review rows are untouched when employees are bulk-deleted, and vice
  versa.
* Malformed hex / empty / missing `ids` → 400 for the unnamed bulk-delete,
  mirroring the iter159 employee contract.

SAFETY: scratch MongoDB database per test, operator-reaped by UUID prefix.
No preview / production data is touched.
"""
import asyncio
import io
import uuid

import httpx
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient
from openpyxl import Workbook
from pymongo.errors import AutoReconnect

import team_score

SCRATCH = "ts_iter160_pytest_scratch"


def _wb_with_unnamed_and_dual_emails() -> bytes:
    """Build a Consolidated-style workbook with the exact headers the
    production template uses: the row-1 merged banner, row-2 headers
    including the two email columns (H + I), and 4 rows — 2 named with
    emails spread between the two columns (one in H only, one in I only),
    and 2 unnamed rows that should route to ts_employees_unnamed."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Consolidated"
    ws.append([None] * 11)  # row 1: merged-banner placeholder
    ws.append([
        "Team",                    # A
        "Employment Status",       # B
        "S.no",                    # C
        "Name",                    # D
        "Joining Date",            # E
        "Relieving Date",          # F
        "Role",                    # G
        "Email ID/ Teams",         # H  ← email column 1
        "Official Email/ Teams ID",# I  ← email column 2
        "LinkedIn ID",             # J
        "LinkedIn",                # K
    ])
    # Alpha: email ONLY in H
    ws.append(["Research Unit", "Active", 1, "Alpha", "", "", "Dev",
               "alpha@blubridge.com", "", "", ""])
    # Beta: email ONLY in I
    ws.append(["Research Unit", "Active", 2, "Beta", "", "", "QA",
               "", "beta@blubridge.com", "", ""])
    # Gamma: unnamed (Name blank) — routes to ts_employees_unnamed
    ws.append(["Research Unit", "Inactive", 3, "", "", "", "Lead",
               "", "", "", ""])
    # Delta: unnamed (Name blank)
    ws.append(["Support Staffs", "Active", 4, "", "", "", "PM",
               "", "", "", ""])
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
                    files={"file": ("x.xlsx",
                                    _wb_with_unnamed_and_dual_emails(),
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


# ─── Admin Import Review deletion ────────────────────────────────────────

def test_unnamed_list_returns_both_captured_rows():
    async def case(tc, _d):
        rows = (await tc.get("/api/team-score/unnamed")).json()["unnamed"]
        assert len(rows) == 2
        # Source row indexes are 0-based positions in the original workbook.
        # Header is row index 1 (after the banner); the two unnamed data rows
        # are rows 4 and 5 (0-based 3 and 4).
        indexes = sorted(r["source_row_index"] for r in rows)
        assert indexes == [4, 5]
        # Both rows carry raw_columns verbatim.
        for r in rows:
            assert "raw_columns" in r and len(r["raw_columns"]) == 11
    run(case)


def test_unnamed_single_delete_removes_only_target():
    async def case(tc, d):
        rows = (await tc.get("/api/team-score/unnamed")).json()["unnamed"]
        assert len(rows) == 2
        target = rows[0]
        r = await tc.delete(f"/api/team-score/unnamed/{target['id']}")
        assert r.status_code == 200
        assert r.json() == {"success": True}
        after = (await tc.get("/api/team-score/unnamed")).json()["unnamed"]
        assert len(after) == 1
        assert after[0]["id"] != target["id"]
        # ts_employees is untouched.
        emps = (await tc.get("/api/team-score/employees")).json()["employees"]
        assert sorted(e["name"] for e in emps) == ["Alpha", "Beta"]
    run(case)


def test_unnamed_single_delete_404_for_unknown_id():
    async def case(tc, _d):
        r = await tc.delete("/api/team-score/unnamed/ffffffffffffffffffffffff")
        assert r.status_code == 404
    run(case)


def test_unnamed_bulk_delete_removes_exact_ids_only():
    async def case(tc, d):
        rows = (await tc.get("/api/team-score/unnamed")).json()["unnamed"]
        ids = [rows[0]["id"], rows[1]["id"]]
        r = await tc.post("/api/team-score/unnamed/bulk-delete",
                          json={"ids": ids})
        assert r.status_code == 200
        body = r.json()
        assert body == {"success": True, "requested": 2, "deleted": 2, "missing": 0}
        after = (await tc.get("/api/team-score/unnamed")).json()["unnamed"]
        assert after == []
        # Employees + rounds untouched.
        emps = (await tc.get("/api/team-score/employees")).json()["employees"]
        assert len(emps) == 2
    run(case)


def test_unnamed_bulk_delete_partial_success():
    async def case(tc, _d):
        rows = (await tc.get("/api/team-score/unnamed")).json()["unnamed"]
        gone = "ffffffffffffffffffffffff"
        r = await tc.post("/api/team-score/unnamed/bulk-delete",
                          json={"ids": [rows[0]["id"], gone]})
        body = r.json()
        assert body["requested"] == 2
        assert body["deleted"] == 1
        assert body["missing"] == 1
    run(case)


def test_unnamed_bulk_delete_rejects_empty_or_malformed():
    async def case(tc, _d):
        assert (await tc.post("/api/team-score/unnamed/bulk-delete", json={})).status_code == 400
        assert (await tc.post("/api/team-score/unnamed/bulk-delete", json={"ids": []})).status_code == 400
        assert (await tc.post("/api/team-score/unnamed/bulk-delete", json={"ids": "not-a-list"})).status_code == 400
        # All malformed → 400 (no valid IDs).
        bad = await tc.post("/api/team-score/unnamed/bulk-delete",
                            json={"ids": ["junk", "also-junk", "", "   "]})
        assert bad.status_code == 400
    run(case)


# ─── Email-column consolidation: both underlying values preserved ─────────

def test_both_email_columns_preserved_in_raw_columns():
    """iter160 — The display layer merges H + I, but the DB keeps BOTH
    verbatim in raw_columns so export + re-import stays lossless. Alpha
    has email in H only, Beta in I only; both values must survive."""
    async def case(tc, _d):
        emps = (await tc.get("/api/team-score/employees")).json()["employees"]
        by_name = {e["name"]: e for e in emps}
        assert set(by_name) == {"Alpha", "Beta"}

        def raw_at(emp, col_idx):
            for rc in emp["raw_columns"]:
                if rc["col"] == col_idx:
                    return rc["value"]
            return None

        # H is column index 7 (0-based), I is column index 8.
        assert raw_at(by_name["Alpha"], 7) == "alpha@blubridge.com"
        assert raw_at(by_name["Alpha"], 8) in ("", None)
        assert raw_at(by_name["Beta"], 7) in ("", None)
        assert raw_at(by_name["Beta"], 8) == "beta@blubridge.com"
        # Canonical fields track H (email) and I (email_teams_id) separately.
        assert by_name["Alpha"]["email"] == "alpha@blubridge.com"
        assert by_name["Alpha"]["email_teams_id"] == ""
        assert by_name["Beta"]["email"] == ""
        assert by_name["Beta"]["email_teams_id"] == "beta@blubridge.com"
    run(case)


def test_schema_exposes_both_email_columns_for_frontend_merge():
    """Frontend consolidation needs to locate columns H + I by their
    original headers — assert both are present at indexes 7 and 8."""
    async def case(tc, _d):
        schema = (await tc.get("/api/team-score/schema")).json()["column_schema"]
        assert len(schema) == 11
        headers = {c["col"]: c["header"].lower().strip() for c in schema}
        assert headers[7] == "email id/ teams"
        assert headers[8] == "official email/ teams id"
        # Letters are preserved for the <th> secondary label.
        letters = {c["col"]: c["letter"] for c in schema}
        assert letters[7] == "H"
        assert letters[8] == "I"
    run(case)


# ─── Cross-feature isolation: employee bulk-delete ≠ unnamed bulk-delete ──

def test_employee_bulk_delete_does_not_touch_unnamed_review():
    async def case(tc, _d):
        emps = (await tc.get("/api/team-score/employees")).json()["employees"]
        before_unnamed = (await tc.get("/api/team-score/unnamed")).json()["unnamed"]
        assert len(before_unnamed) == 2

        r = await tc.post("/api/team-score/employees/bulk-delete",
                          json={"ids": [emps[0]["id"]]})
        assert r.status_code == 200
        assert r.json()["deleted"] == 1

        after_unnamed = (await tc.get("/api/team-score/unnamed")).json()["unnamed"]
        assert len(after_unnamed) == 2  # unchanged
    run(case)


def test_unnamed_bulk_delete_does_not_touch_employees_or_rounds():
    async def case(tc, d):
        emps_before = (await tc.get("/api/team-score/employees")).json()["employees"]
        rounds_before = await d.ts_rounds.count_documents({})
        rows = (await tc.get("/api/team-score/unnamed")).json()["unnamed"]
        r = await tc.post("/api/team-score/unnamed/bulk-delete",
                          json={"ids": [u["id"] for u in rows]})
        assert r.status_code == 200
        emps_after = (await tc.get("/api/team-score/employees")).json()["employees"]
        assert sorted(e["name"] for e in emps_after) == sorted(e["name"] for e in emps_before)
        # Round catalogue untouched by the unnamed bulk-delete.
        assert await d.ts_rounds.count_documents({}) == rounds_before
    run(case)
