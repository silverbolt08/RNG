"""The fixed API contract, exercised over HTTP on the real dataset.

Starts the application exactly as uvicorn does (startup precompute included),
offline, against a throwaway store.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from rng.config import settings


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("store")
    mp = pytest.MonkeyPatch()
    mp.setenv("DUCKDB_PATH", str(tmp / "test.duckdb"))
    mp.setenv("DATA_DIR", "data/primary")
    mp.setenv("MOCK_LLM", "true")
    settings.cache_clear()
    from rng.api.main import app

    with TestClient(app) as c:
        yield c
    mp.undo()
    settings.cache_clear()


def test_health_reports_api_store_and_llm(client):
    body = client.get("/health").json()
    assert {"status", "api", "data_store", "llm"} <= set(body)
    assert body["api"]["status"] == "ok"
    assert body["llm"]["status"] == "ok" and body["mock_llm"] is True
    assert "meter rows" in body["data_store"]["detail"]


def test_analyze_returns_the_section_6_1_shape(client):
    r = client.post("/analyze", json={"question": "Which assets use the most energy?",
                                      "period": "2026-Q3"})
    assert r.status_code == 200
    body = r.json()
    assert {"run_id", "answer_type", "summary", "bridge", "recommendations",
            "assumptions", "limitations"} <= set(body)
    assert body["answer_type"] == "calculated"
    assert body["run_id"].startswith("en-")


def test_analyze_question_only_is_enough(client):
    r = client.post("/analyze", json={"question": "What will the tariff be next year?"})
    assert r.status_code == 200 and r.json()["answer_type"] == "not_in_data"


def test_analyze_rejects_a_missing_question(client):
    assert client.post("/analyze", json={}).status_code == 422


def test_trace_returns_the_run_and_404s_on_an_unknown_id(client):
    run_id = client.post("/analyze", json={
        "question": "Which assets use the most energy?"}).json()["run_id"]
    t = client.get(f"/trace/{run_id}").json()
    assert t["run_id"] == run_id and t["steps"]
    assert {"agent", "tool"} <= set(t["steps"][0])
    assert "llm_calls" in t and "tokens" in t
    assert client.get("/trace/en-000000").status_code == 404


def _tariff_csv() -> bytes:
    return (b"valid_from,valid_to,period,price_per_kwh\n"
            b"2026-04-01T00:00:00Z,2026-10-01T00:00:00Z,flat,6.5\n")


@pytest.mark.parametrize("fields", [
    {"data": {"table": "tariff"}},
    {"data": {"meta": '{"table": "tariff"}'}},
    {"params": {"table": "tariff"}},
    {},                                            # falls back to the file name
])
def test_ingest_accepts_the_table_name_however_it_is_sent(client, fields):
    r = client.post("/ingest", files={"file": ("tariff.csv", io.BytesIO(_tariff_csv()),
                                               "text/csv")}, **fields)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"rows_loaded", "rows_rejected", "status"} <= set(body)
    assert body["status"] == "ok" and body["rows_loaded"] == 1


def test_ingest_reports_a_file_it_cannot_use(client):
    r = client.post("/ingest", data={"table": "weather"},
                    files={"file": ("weather.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")})
    assert r.status_code == 200
    assert r.json()["status"] == "failed" and r.json()["rows_loaded"] == 0


def test_ingest_without_a_file_is_a_422(client):
    assert client.post("/ingest", data={"table": "tariff"}).status_code == 422


def test_the_system_still_answers_after_an_ingest(client):
    r = client.post("/analyze", json={"question": "Which assets use the most energy?"})
    assert r.status_code == 200 and r.json()["answer_type"] == "calculated"
