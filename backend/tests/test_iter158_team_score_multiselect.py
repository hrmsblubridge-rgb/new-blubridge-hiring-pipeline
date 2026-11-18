"""iter158 — Team Score multi-select employee filter regression."""
import asyncio
import io
import uuid

import httpx
from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient
from openpyxl import Workbook
from pymongo.errors import AutoReconnect

import team_score

SCRATCH = "ts_multi_pytest_scratch"


def _wb() -> bytes:
    wb = Workbook(); ws = wb.active; ws.title = "Consolidated"
    ws.append([None] * 5)
    ws.append(["Team", "Employment Status", "S.no", "Name", "Email ID/ Teams"])
    rows = [
        ["Research Unit", "Active",   1, "Sanjay Krishna MV", "sanjay.krishna@blubridge.com"],
        ["Research Unit", "Active",   2, "Sanjay Kumar",      "sanjaykumar@blubridge.com"],
        ["Research Unit", "Inactive", 3, "V Sanjay Kumar",    ""],
        ["Business Team", "Active",   4, "Kathirvel S",       "kathirvel.s@blubridge.com"],
        ["Support Staffs","Inactive", 5, "Kathiravan",        ""],
    ]
    for r in rows: ws.append(r)
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
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as tc:
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


def test_employee_ids_or_filter_selects_only_listed_ids():
    async def case(tc, _d):
        # Pick two Sanjays via /suggest
        r = (await tc.get("/api/team-score/suggest?q=sanjay")).json()
        ids = [s["id"] for s in r["suggestions"][:2]]
        assert len(ids) == 2
        emp = (await tc.get(f"/api/team-score/employees?employee_ids={','.join(ids)}")).json()
        assert emp["filtered_count"] == 2
        got = {e["name"] for e in emp["employees"]}
        assert got.issubset({"Sanjay Krishna MV", "Sanjay Kumar", "V Sanjay Kumar"})
        assert len(got) == 2
    run(case)


def test_employee_ids_ANDs_with_team_and_status():
    async def case(tc, _d):
        ids = [s["id"] for s in (await tc.get("/api/team-score/suggest?q=sanjay")).json()["suggestions"]]
        assert len(ids) == 3
        # AND with status=active drops V Sanjay Kumar (inactive)
        emp = (await tc.get(f"/api/team-score/employees?employee_ids={','.join(ids)}&employee_status=active")).json()
        assert emp["filtered_count"] == 2
        got = {e["name"] for e in emp["employees"]}
        assert got == {"Sanjay Krishna MV", "Sanjay Kumar"}
        # AND with a team the selected employees don't belong to → 0
        emp2 = (await tc.get(f"/api/team-score/employees?employee_ids={','.join(ids)}&team=Business Team")).json()
        assert emp2["filtered_count"] == 0
    run(case)


def test_malformed_employee_ids_are_dropped_not_500():
    async def case(tc, _d):
        valid = (await tc.get("/api/team-score/suggest?q=kathirvel")).json()["suggestions"][0]["id"]
        emp = (await tc.get(f"/api/team-score/employees?employee_ids=junk,{valid},also-junk")).json()
        assert emp["filtered_count"] == 1
        assert emp["employees"][0]["name"] == "Kathirvel S"
    run(case)


def test_empty_employee_ids_returns_full_filtered_list():
    async def case(tc, _d):
        empty = (await tc.get("/api/team-score/employees?employee_ids=")).json()
        all_ = (await tc.get("/api/team-score/employees")).json()
        assert empty["filtered_count"] == all_["filtered_count"] == 5
    run(case)
