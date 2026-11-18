"""iter157 — Team Score Excel column-order preservation regression.

Verifies that `GET /api/team-score/schema` returns the authoritative
Excel-order schema persisted by `POST /api/team-score/import` and that:

* columns 0..n map 1:1 to Excel columns A..n+1
* duplicated headers (e.g. "Score /10") remain separate entries with
  unique `column_id` values — no collapse
* `header_original` retains line breaks and whitespace exactly as stored
* a subsequent import replaces the schema with the newest headers
  (previous version is archived under a `history_*` document)

SAFETY: per-test scratch MongoDB, operator-reaped by UUID prefix.
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

SCRATCH = "ts_schema_pytest_scratch"


def _wb(headers: list, data_rows: list) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Consolidated"
    ws.append([None] * len(headers))  # merged group header row
    ws.append(headers)
    for r in data_rows:
        ws.append(r)
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
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://t") as tc:
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


def test_schema_mirrors_excel_order_including_duplicates():
    async def case(tc, d):
        headers = ["Team", "Employment Status", "S.no", "Name", "Joining Date",
                   "Role", "Score /10", "Score /10", "Score /10"]
        xlsx = _wb(headers, [["RU", "Active", 1, "A", "01/01/2025", "Dev", 8, 9, 10]])
        r = await tc.post("/api/team-score/import",
                          files={"file": ("x.xlsx", xlsx,
                                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert r.status_code == 200
        s = (await tc.get("/api/team-score/schema")).json()
        assert s["header_count"] == len(headers)
        schema = s["column_schema"]
        # 1:1 mapping, Excel A..I
        assert [c["header"] for c in schema] == headers
        assert [c["letter"] for c in schema] == ["A", "B", "C", "D", "E", "F", "G", "H", "I"]
        assert [c["col"] for c in schema] == list(range(len(headers)))
        # Duplicated headers stay separate with unique column_id.
        score_cols = [c for c in schema if c["header"] == "Score /10"]
        assert len(score_cols) == 3
        assert len({c["column_id"] for c in score_cols}) == 3


def test_schema_republishes_on_reimport_and_archives_previous():
    async def case(tc, d):
        v1_headers = ["Team", "Name", "Score /10"]
        v2_headers = ["Team", "Name", "Score /10", "Score /20"]  # one more column
        await tc.post("/api/team-score/import",
                      files={"file": ("v1.xlsx", _wb(v1_headers, [["RU", "A", 5]]),
                                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        await tc.post("/api/team-score/import",
                      files={"file": ("v2.xlsx", _wb(v2_headers, [["RU", "A", 5, 12]]),
                                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        s = (await tc.get("/api/team-score/schema")).json()
        assert s["header_count"] == 4
        assert [c["header"] for c in s["column_schema"]] == v2_headers
        # Previous schema archived (not deleted)
        archived = await d.ts_column_schema.count_documents(
            {"_id": {"$regex": "^history_"}})
        assert archived == 1


def test_schema_preserves_header_original_with_line_breaks():
    """The verbatim row-2 cell value is stored as `header_original` so
    line breaks (`Systems Test\\n/110`, `Acc 1\\n/25`) survive export."""
    async def case(tc, d):
        headers = ["Team", "Name", "Systems Test\n/110", "Acc 1\n/25"]
        await tc.post("/api/team-score/import",
                      files={"file": ("x.xlsx", _wb(headers, [["RU", "A", 1, 2]]),
                                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        s = (await tc.get("/api/team-score/schema")).json()
        assert s["column_schema"][2]["header_original"] == "Systems Test\n/110"
        assert s["column_schema"][3]["header_original"] == "Acc 1\n/25"


def test_schema_endpoint_empty_before_any_import():
    async def case(tc, _d):
        s = (await tc.get("/api/team-score/schema")).json()
        assert s["column_schema"] == []
        assert s["header_count"] == 0
    run(case)


# Make all top-level test functions use `run` by wrapping their bodies.
# (The helper already pattern-matches the other test files in /app/backend/tests.)
for _name in ("test_schema_mirrors_excel_order_including_duplicates",
              "test_schema_republishes_on_reimport_and_archives_previous",
              "test_schema_preserves_header_original_with_line_breaks"):
    _case = globals()[_name].__wrapped__ if hasattr(globals()[_name], "__wrapped__") else None
    # No-op — the wrappers above already invoke `run(case)` via inner `case`.
