"""The frozen contract: api/schemas.py and store/schema.sql.

Three other workstreams build against these. A careless edit should break a
test here rather than break three people quietly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from rng.api.schemas import (
    AnalyzeRequest,
    AnalyzeResponse,
    AnswerType,
    BridgeItem,
    Cause,
    Confidence,
    Ease,
    EstimatedImpact,
    HealthResponse,
    IngestResponse,
    Recommendation,
    TraceResponse,
    TraceStep,
)

ROOT = Path(__file__).resolve().parents[1]


def _impact() -> EstimatedImpact:
    return EstimatedImpact(
        saving_kwh_per_year=(120000, 160000),
        saving_money_per_year=(14400, 19200),
        saving_co2e_t_per_year=(60, 80),
        payback_months=(3, 6),
        ease=Ease.EASY,
    )


def _recommendation() -> Recommendation:
    return Recommendation(
        action="Repair compressor air leak on Line 3",
        rationale="Night-time load on compressors stays at 38% of day load.",
        evidence_refs=["meter_15min:compressors:2026-Q3"],
        confidence=Confidence.MEDIUM,
        estimated_impact=_impact(),
        requires_approval=True,
    )


# -- the wire format -------------------------------------------------------

def test_analyze_response_carries_every_key_section_6_1_shows():
    r = AnalyzeResponse(
        run_id="en-5d20ab",
        answer_type=AnswerType.DECOMPOSITION,
        summary="The bill rose 15.0%.",
        bridge=[BridgeItem(cause=Cause.TARIFF, pct_points=15.0,
                           range=(14.0, 16.0))],
        recommendations=[_recommendation()],
        assumptions=["Emission factor 0.71 kg CO2e per kWh"],
        limitations=["Weather effect estimated from one quarter of hot days"],
        total_pct_points=15.0,
    )
    body = r.model_dump(mode="json")
    assert {"run_id", "answer_type", "summary", "bridge", "recommendations",
            "assumptions", "limitations"} <= set(body)
    assert set(body["bridge"][0]) >= {"cause", "pct_points", "range"}
    assert body["bridge"][0]["range"] == [14.0, 16.0]   # a JSON array, not an object


def test_the_mix_cause_is_spelled_product_mix_on_the_wire():
    """Section 6.1 says product_mix. The generator says mix internally."""
    assert Cause.PRODUCT_MIX.value == "product_mix"
    assert {c.value for c in Cause} == {
        "tariff", "product_mix", "volume", "weather", "waste"}


def test_answer_types_are_exactly_the_six_in_section_6_2():
    assert {a.value for a in AnswerType} == {
        "decomposition", "recommendation", "calculated",
        "not_in_data", "clarification_needed", "refused"}


def test_recommendation_emits_impact_both_nested_and_flat():
    body = _recommendation().model_dump(mode="json")
    assert {"action", "rationale", "evidence_refs", "confidence",
            "estimated_impact", "requires_approval"} <= set(body)
    assert body["saving_kwh_per_year"] == [120000, 160000]
    assert body["estimated_impact"]["saving_kwh_per_year"] == [120000, 160000]
    assert body["payback_months"] == [3, 6] and body["ease"] == "easy"


# -- the guards ------------------------------------------------------------

def test_a_recommendation_without_evidence_is_rejected():
    with pytest.raises(ValidationError):
        Recommendation(
            action="Do something", rationale="Because",
            evidence_refs=[], confidence=Confidence.LOW,
            estimated_impact=_impact())


def test_an_inverted_range_is_rejected():
    with pytest.raises(ValidationError):
        BridgeItem(cause=Cause.WASTE, pct_points=1.0, range=(2.0, 1.0))


def test_a_bridge_that_does_not_add_up_is_rejected():
    causes = [(Cause.TARIFF, 7.4), (Cause.PRODUCT_MIX, 2.9),
              (Cause.VOLUME, 1.6), (Cause.WEATHER, 1.6), (Cause.WASTE, 1.5)]
    good = [BridgeItem(cause=c, pct_points=v, range=(v - 0.5, v + 0.5))
            for c, v in causes]
    AnalyzeResponse(run_id="r", answer_type=AnswerType.DECOMPOSITION,
                    summary="s", bridge=good, total_pct_points=15.0)
    with pytest.raises(ValidationError):
        AnalyzeResponse(run_id="r", answer_type=AnswerType.DECOMPOSITION,
                        summary="s", bridge=good[:-1], total_pct_points=15.0)


def test_the_other_three_endpoints_round_trip():
    AnalyzeRequest(question="Why did the bill go up 15%?", period="2026-Q3",
                   compare_to="2026-Q2")
    IngestResponse(table="meter_15min", rows_loaded=10, rows_rejected=0,
                   status="ok")
    HealthResponse(status="ok", api={"status": "ok"},
                   data_store={"status": "ok"}, llm={"status": "ok"})
    t = TraceResponse(
        run_id="en-5d20ab", question="Why did the bill go up 15%?",
        steps=[TraceStep(agent="analyst", tool="build_baseline",
                         output={"r2": 0.91}),
               TraceStep(agent="writer", tool="write_finance_brief",
                         check="all numbers match tool outputs")],
        llm_calls=1, tokens=1600)
    assert t.model_dump(mode="json")["steps"][1]["check"]


# -- the store -------------------------------------------------------------

def test_schema_sql_runs_and_is_idempotent():
    duckdb = pytest.importorskip("duckdb")
    sql = (ROOT / "rng" / "store" / "schema.sql").read_text(encoding="utf-8")
    con = duckdb.connect(":memory:")
    con.execute(sql)
    con.execute(sql)                       # a reload through /ingest must be safe
    tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    assert {"meter_15min", "tariff", "production", "weather", "datasets",
            "meter_clean", "analysis_cache", "runs", "run_steps",
            "registry_values"} <= tables


def test_timestamps_come_back_in_utc_whatever_the_machine_time_zone():
    """Midnight UTC must read back as hour 0, on a laptop and in the container.

    If it reads back in local time the peak window shifts and the tariff term
    is mispriced without any error being raised.
    """
    pytest.importorskip("duckdb")
    from rng.store.db import connect, init_schema

    con = connect(":memory:")
    init_schema(con)
    con.execute("INSERT INTO meter_15min VALUES "
                "('main', TIMESTAMPTZ '2026-04-01T00:00:00Z', 1.0)")
    ts = con.execute("SELECT ts FROM meter_15min").fetchone()[0]
    assert ts.hour == 0 and ts.utcoffset().total_seconds() == 0
    assert con.execute("SELECT hour(ts) FROM meter_15min").fetchone()[0] == 0


def test_cache_holds_single_period_and_paired_results_side_by_side():
    """Top-users needs one period; the bridge needs a pair. Both must fit,
    two single-period kinds must not collide, and a rewrite must replace."""
    pytest.importorskip("duckdb")
    from rng.store.db import connect, init_schema

    con = connect(":memory:")
    init_schema(con)
    insert = ("INSERT OR REPLACE INTO analysis_cache "
              "(dataset_id, kind, period, compare_to, payload, computed_at) "
              "VALUES (?, ?, ?, ?, ?, now())")
    con.execute(insert, ["d", "asset_totals", "2026-Q3", "", "{}"])
    con.execute(insert, ["d", "idle_load", "2026-Q3", "", "{}"])
    con.execute(insert, ["d", "bridge", "2026-Q3", "2026-Q2", "{}"])
    con.execute(insert, ["d", "bridge", "2026-Q3", "2026-Q2", '{"v": 2}'])
    rows = con.execute("SELECT kind, payload FROM analysis_cache "
                       "ORDER BY kind").fetchall()
    assert [r[0] for r in rows] == ["asset_totals", "bridge", "idle_load"]
    assert rows[1][1] == '{"v": 2}'


def test_raw_tables_match_annex_b():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(":memory:")
    con.execute((ROOT / "rng" / "store" / "schema.sql").read_text(encoding="utf-8"))
    required = {
        "meter_15min": {"meter_id", "ts", "kwh"},
        "tariff": {"valid_from", "valid_to", "period", "price_per_kwh"},
        "production": {"date", "product", "units", "shift_pattern", "is_holiday"},
        "weather": {"date", "avg_temp_c", "cooling_degree_days",
                    "heating_degree_days"},
    }
    for table, columns in required.items():
        got = {r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()}
        assert columns <= got, f"{table} missing {columns - got}"


# -- the declared assumptions ---------------------------------------------

def test_assumptions_come_from_the_data_pack_when_it_supplies_them():
    from rng.config import assumptions

    a = assumptions()
    assert a.emission_factor_kg_co2e_per_kwh > 0
    statements = a.as_statements()
    assert any("Emission factor" in s for s in statements)
    assert any("Peak tariff hours" in s for s in statements)
    # Fix costs are ours and must say so -- Annex A, and CLAUDE.md section 5.
    assert any("not present in any input file" in s for s in statements)


# -- shared helpers every workstream uses ----------------------------------

def test_period_labels_parse_to_half_open_bounds():
    from datetime import date

    from rng.periods import available_periods, period_bounds, previous_period

    assert period_bounds("2026-Q3") == (date(2026, 7, 1), date(2026, 10, 1))
    assert period_bounds("2026-Q4") == (date(2026, 10, 1), date(2027, 1, 1))
    assert period_bounds("2026-02") == (date(2026, 2, 1), date(2026, 3, 1))
    assert previous_period("2026-Q1") == "2025-Q4"
    assert available_periods(["2026-04-01", "2026-09-30T23:45:00Z"]) == [
        "2026-Q2", "2026-Q3"]
    with pytest.raises(ValueError):
        period_bounds("next year")


def test_bridge_exposes_the_wire_format_and_checks_additivity():
    from rng.analysis.decompose import Bridge

    effects = {Cause.TARIFF: 7.4, Cause.PRODUCT_MIX: 2.9, Cause.VOLUME: 1.6,
               Cause.WEATHER: 1.6, Cause.WASTE: 1.5}
    bridge = Bridge(period="2026-Q3", compare_to="2026-Q2",
                    reference_cost=100.0, current_cost=115.0, effects=effects)
    bridge.assert_adds_up()
    assert bridge.total_pct == pytest.approx(15.0)
    assert bridge.waste_pp == pytest.approx(1.5)
    items = bridge.to_items()
    assert [i.cause for i in items] == list(Cause)
    AnalyzeResponse(run_id="r", answer_type=AnswerType.DECOMPOSITION,
                    summary="s", bridge=items,
                    total_pct_points=bridge.total_pct)

    broken = Bridge(period="2026-Q3", compare_to="2026-Q2",
                    reference_cost=100.0, current_cost=115.0,
                    effects={**effects, Cause.WASTE: 0.0})
    with pytest.raises(AssertionError):
        broken.assert_adds_up()


def test_every_module_contract_imports():
    """The stubs exist so four people integrate against one set of signatures."""
    from rng.analysis.assets import asset_totals
    from rng.analysis.baseline import fit_baseline
    from rng.analysis.decompose import decompose
    from rng.analysis.savings import estimate_savings
    from rng.analysis.waste import detect_leaks, find_idle_load
    from rng.ingest.clean import clean
    from rng.ingest.loader import REQUIRED_COLUMNS, load_csv, load_dir
    from rng.ingest.tariff import resolve_prices

    assert set(REQUIRED_COLUMNS) == {
        "meter_15min", "tariff", "production", "weather"}
    assert all(callable(f) for f in (
        asset_totals, fit_baseline, decompose, estimate_savings, detect_leaks,
        find_idle_load, clean, load_csv, load_dir, resolve_prices))


def test_leak_detection_cannot_be_called_before_the_bridge_exists():
    """Annex A's order of attribution, enforced by the signature."""
    import inspect

    from rng.analysis.waste import detect_leaks

    params = inspect.signature(detect_leaks).parameters
    assert list(params)[:3] == ["clean", "bridge", "idle"]
    assert params["bridge"].default is inspect.Parameter.empty
