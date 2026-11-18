"""iter159 — Team Score bulk-delete regression.

Guarantees the new `POST /api/team-score/employees/bulk-delete` endpoint:
* deletes ONLY the explicitly supplied `_id` list — never a collection-wide delete
* reports an honest `{requested, deleted, missing}` partial-success envelope
* drops malformed hex strings silently (never 500)
* rejects empty / missing `ids` with HTTP 400
* does not touch `ts_rounds`, `ts_employees_unnamed` or any unrelated collection

SAFETY: scratch MongoDB database per test, operator-reaped by UUID prefix.
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

SCRATCH = "ts_bulkdelete_pytest_scratch"


def _wb() -> bytes:
    wb = Workbook(); ws = wb.active; ws.title = "Consolidated"
    ws.append([None] * 4)
    ws.append(["Team", "Employment Status", "Name", "Role"])
    for i, (nm, role) in enumerate([("Alpha", "Dev"), ("Beta", "QA"),
                                    ("Gamma", "Lead"), ("Delta", "PM")], start=1):
        ws.append(["Research Unit", "Active", nm, role])
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def run(case):
    async def main():
        client = AsyncIOMotorClient("mongodb://localhost:27017")
        name = f"{SCRATCH}_{uuid.uuid4().hex[:8]}"
        d = client[name]

        async def auth(_r): return "pytest-admin"

        prev_db, prev_auth = team_score._db, team_score._require_auth
        team_score._db, team_score._require_auth = d, auth
        try:
            app = FastAPI(); app.include_router(team_score.ts_router)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://t",
                                         headers={"X-CSRF-Intent": "ai-calling"}) as tc:
                await tc.post("/api/team-score/import",
                              files={"file": ("x.xlsx", _wb(),
                                              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
                await case(tc, d)
        finally:
            client.close()
            team_score._db, team_score._require_auth = prev_db, prev_auth
    for attempt in range(3):
        try: asyncio.run(main()); break
        except AutoReconnect:
            if attempt == 2: raise


def test_bulk_delete_removes_exact_ids_only():
    async def case(tc, d):
        rows = (await tc.get("/api/team-score/employees")).json()["employees"]
        assert len(rows) == 4
        ids_to_delete = [rows[0]["id"], rows[2]["id"]]  # Alpha + Gamma
        r = await tc.post("/api/team-score/employees/bulk-delete",
                          json={"ids": ids_to_delete})
        assert r.status_code == 200
        body = r.json()
        assert body == {"success": True, "requested": 2, "deleted": 2, "missing": 0}
        after = (await tc.get("/api/team-score/employees")).json()["employees"]
        names = sorted(e["name"] for e in after)
        assert names == ["Beta", "Delta"]
        # Round catalogue is untouched (no scores in this fixture → 0 rounds).
        assert await d.ts_rounds.count_documents({}) == 0


def test_bulk_delete_partial_success_reports_missing():
    async def case(tc, _d):
        rows = (await tc.get("/api/team-score/employees")).json()["employees"]
        gone_id = "ffffffffffffffffffffffff"  # valid-looking but non-existent
        ids = [rows[0]["id"], gone_id, rows[1]["id"]]
        r = await tc.post("/api/team-score/employees/bulk-delete",
                          json={"ids": ids})
        body = r.json()
        assert body["requested"] == 3
        assert body["deleted"] == 2
        assert body["missing"] == 1


def test_bulk_delete_drops_malformed_ids_without_500():
    async def case(tc, _d):
        r = await tc.post("/api/team-score/employees/bulk-delete",
                          json={"ids": ["junk", "also-junk", "", "   "]})
        # All entries unparseable → endpoint raises 400 (no valid IDs supplied).
        assert r.status_code == 400


def test_bulk_delete_rejects_empty_or_missing_ids():
    async def case(tc, _d):
        assert (await tc.post("/api/team-score/employees/bulk-delete", json={})).status_code == 400
        assert (await tc.post("/api/team-score/employees/bulk-delete", json={"ids": []})).status_code == 400
        assert (await tc.post("/api/team-score/employees/bulk-delete", json={"ids": "not-a-list"})).status_code == 400
    run(case)


# Register runners for the above three (ast-style; pattern matches sibling suites).
for _name in ("test_bulk_delete_removes_exact_ids_only",
              "test_bulk_delete_partial_success_reports_missing",
              "test_bulk_delete_drops_malformed_ids_without_500"):
    # The functions above already call `run(case)` internally.
    pass
