# iter160 — LIVE non-destructive checks against preview: schema order, unnamed delete error paths (no real ids touched)
import os
import pytest
import requests

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL').rstrip('/')
TS = f"{BASE_URL}/api/team-score"
UNKNOWN = "000000000000000000000000"


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    r = sess.post(f"{BASE_URL}/api/login", json={"username": "Admin User", "password": "QAZwsx##123"})
    if r.status_code != 200:
        pytest.skip(f"login failed {r.status_code} {r.text[:200]}")
    tok = r.json().get("token") or r.json().get("access_token")
    if tok:
        sess.headers["Authorization"] = f"Bearer {tok}"
    return sess


@pytest.fixture(scope="module")
def baseline(s):
    e = s.get(f"{TS}/employees").json()
    u = s.get(f"{TS}/unnamed").json()["unnamed"]
    r = s.get(f"{TS}/rounds").json()["rounds"]
    return {"emp": e.get("total_count", len(e["employees"])), "unnamed": len(u), "rounds": len(r)}


# Schema keeps 114 cols & H/I/J/K order
def test_schema(s):
    cs = s.get(f"{TS}/schema").json()["column_schema"]
    assert len(cs) == 114
    assert cs[7]["header"] == "Email ID/ Teams"
    assert cs[8]["header"] == "Official Email/ Teams ID"
    assert cs[9]["header"] == "LinkedIn ID"
    assert cs[10]["header"] == "LinkedIn"


def test_unnamed_list(s, baseline):
    u = s.get(f"{TS}/unnamed").json()["unnamed"]
    assert len(u) == 2
    assert sorted(x["source_row_index"] for x in u) in ([185, 186], [186, 187])
    assert all("_id" not in x and isinstance(x["id"], str) for x in u)


def test_delete_unknown_404(s):
    r = s.delete(f"{TS}/unnamed/{UNKNOWN}")
    assert r.status_code == 404


def test_delete_malformed_400(s):
    r = s.delete(f"{TS}/unnamed/not-an-id")
    assert r.status_code == 400


@pytest.mark.parametrize("body", [{"ids": []}, {}, {"ids": "abc"}, {"ids": ["bad", "x"]}])
def test_bulk_rejects(s, body):
    r = s.post(f"{TS}/unnamed/bulk-delete", json=body)
    assert r.status_code == 400, r.text


def test_bulk_unknown_envelope(s):
    r = s.post(f"{TS}/unnamed/bulk-delete", json={"ids": [UNKNOWN]})
    assert r.status_code == 200
    d = r.json()
    assert d == {"success": True, "requested": 1, "deleted": 0, "missing": 1}


def test_unauth_rejected():
    r = requests.post(f"{TS}/unnamed/bulk-delete", json={"ids": [UNKNOWN]})
    assert r.status_code in (401, 403)


def test_counts_unchanged(s, baseline):
    e = s.get(f"{TS}/employees").json()
    assert e.get("total_count", len(e["employees"])) == baseline["emp"] == 183
    assert len(s.get(f"{TS}/unnamed").json()["unnamed"]) == baseline["unnamed"]
    assert len(s.get(f"{TS}/rounds").json()["rounds"]) == baseline["rounds"]
