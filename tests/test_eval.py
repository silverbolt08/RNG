"""The evaluation harness, run against a stand-in API.

The stand-in answers over HTTP in the frozen AnalyzeResponse shape: once
correctly (built from the truth) and then wrong in specific ways. A harness
that passes a wrong system is worse than no harness, so each failure mode the
brief penalises is shown to be caught.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from rng.analysis.savings import estimate_savings
from rng.api.schemas import AnalyzeResponse, Confidence, Finding
from rng.eval.cases import (
    CAUSES,
    HOURS_PER_YEAR,
    MINIMUMS,
    WIRE_NAME,
    build_cases,
    load_context,
    numerals,
)
from rng.eval.run_eval import evaluate, writer_path

ROOT = Path(__file__).resolve().parents[1]


def _bridge(ctx, tariff_shift: float = 0.0):
    items, registry = [], {}
    for short, wire in WIRE_NAME.items():
        pp = round(ctx.true_pp[wire] + (tariff_shift if wire == "tariff" else 0.0), 2)
        items.append({"cause": wire, "pct_points": pp,
                      "range": [pp - 0.3, pp + 0.3],
                      "amount": ctx.truth["bridge_true"][short]["cost"]})
        registry[f"{wire}_pp"] = pp
    total = round(ctx.rise_pct + tariff_shift, 2)
    registry["total_pp"] = total
    return items, total, registry


def _answer(case, ctx) -> tuple[dict, dict]:
    """What a correct system would return for `case`: (response, registry)."""
    out = {"answer_type": case.expected[0], "summary": "", "bridge": [],
           "recommendations": [], "assumptions": ["a"], "limitations": ["l"]}
    registry: dict[str, float] = {}
    kind = out["answer_type"]
    if case.case_id == "I3":
        out["answer_type"] = kind = "not_in_data"

    if kind == "decomposition" or case.case_id == "N1":
        out["bridge"], out["total_pct_points"], registry = _bridge(ctx)
        top = ctx.top_cause
        out["summary"] = (f"The bill changed by {out['total_pct_points']:.2f}%; "
                          f"the largest cause was {top} at "
                          f"{registry[f'{top}_pp']:.2f} percentage points.")
    elif case.case_id in ("W1", "W2"):
        out["bridge"], out["total_pct_points"], registry = _bridge(ctx, -2.0)
        out["summary"] = f"The change would have been {out['total_pct_points']:.2f}%."
    elif case.case_id in ("N3", "N4"):
        name, kwh = next(iter(ctx.facts["asset_kwh"][case.period].items()))
        registry = {"top_kwh": kwh}
        out["summary"] = f"{name} used the most energy: {kwh:,.0f} kWh."
    elif kind == "recommendation":
        leak = ctx.leak
        finding = Finding(
            finding_id="f1", asset=leak["meter_id"], kind="leak",
            description=f"Night load on {leak['meter_id']} stepped up",
            kwh_per_year=leak["kw"] * HOURS_PER_YEAR,
            evidence_refs=[f"meter_clean:{leak['meter_id']}:{ctx.period}"],
            confidence=Confidence.HIGH)
        recs = estimate_savings([finding], price_per_kwh=5.0)
        out["recommendations"] = [r.model_dump(mode="json") for r in recs]
        out["summary"] = f"Start with {leak['meter_id']}."
    elif kind == "calculated":
        registry = {"idle_kwh": 1234.5}
        out["summary"] = "Idle load was 1,234.5 kWh."
    else:
        out["summary"] = "The loaded data cannot answer that."
    return out, registry


def _api(ctx, tamper=None) -> httpx.Client:
    cases = {c.question: c for c in build_cases(ctx)}
    traces: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={
                "status": "ok", "mock_llm": True,
                "dataset_id": ctx.truth["dataset_id"]})
        if path.startswith("/trace/"):
            return httpx.Response(200, json=traces[path.rsplit("/", 1)[1]])
        body = json.loads(request.content)
        case = cases[body["question"]]
        assert body.get("period") == case.period
        response, registry = _answer(case, ctx)
        response["run_id"] = f"run-{case.case_id}"
        if tamper:
            tamper(case, response, registry)
        # The stand-in must itself honour the frozen contract.
        response = AnalyzeResponse.model_validate(response).model_dump(mode="json")
        traces[response["run_id"]] = {
            "run_id": response["run_id"], "question": case.question,
            "answer_type": response["answer_type"], "llm_calls": 2,
            "tokens": 300, "latency_ms": 12.0,
            "steps": [{"agent": "writer", "tool": "verify_numbers",
                       "output": {"unmatched": 0, "writer": "llm"}}],
            "registry": [{"key": k, "value": v, "unit": "x", "source_tool": "t",
                          "evidence_ref": "e"} for k, v in registry.items()]}
        return httpx.Response(200, json=response)

    return httpx.Client(transport=httpx.MockTransport(handler),
                        base_url="http://test")


@pytest.fixture(scope="module", params=("primary", "holdout"))
def ctx(request):
    return load_context(ROOT / "data" / request.param)


def _run(ctx, tamper=None):
    data_dir = ROOT / "data" / ctx.truth["profile"]
    with _api(ctx, tamper) as client:
        return evaluate(client, data_dir, "http://test")


def test_the_set_meets_the_section_10_minimums(ctx):
    cases = build_cases(ctx)
    assert len(cases) >= 20
    assert len({c.case_id for c in cases}) == len(cases)
    for category, minimum in MINIMUMS.items():
        assert sum(c.category == category for c in cases) >= minimum


def test_no_period_is_written_into_the_cases(ctx):
    """Relabel the truth: every period a case sends must follow it."""
    moved = type(ctx)(truth={**ctx.truth, "current_period": "2019-Q2",
                             "reference_period": "2019-Q1"},
                      facts={**ctx.facts, "asset_kwh": {
                          "2019-Q2": {"x": 1.0}, "2019-Q1": {"x": 1.0}}})
    sent = {c.period for c in build_cases(moved)} | {
        c.compare_to for c in build_cases(moved)}
    assert sent == {None, "2019-Q2", "2019-Q1", "2024-Q1"}


def test_a_correct_system_passes_every_case(ctx):
    report, metrics, outcomes = _run(ctx)
    failed = [(o.case.case_id, o.detail) for o in outcomes if not o.passed]
    assert not failed, failed
    assert metrics["reconciliation"]["passed"] == metrics["reconciliation"]["of"] > 0
    assert all(metrics["decomposition"][c]["mae_pp"] < 0.01 for c in CAUSES)
    assert metrics["waste"]["leak_found"] and metrics["savings"]["in_range"]
    assert metrics["faithfulness"]["faithful"] == metrics["faithfulness"]["traced"]
    assert metrics["faithfulness"]["unaided"] == metrics["cases"]
    assert metrics["llm"]["calls_max"] == 2
    assert "## Decomposition error" in report and "| D1 |" in report


def test_blaming_a_machine_first_is_caught(ctx):
    def swap(case, response, registry):
        by_cause = {i["cause"]: i for i in response["bridge"]}
        if by_cause:
            top, waste = by_cause[ctx.top_cause], by_cause["waste"]
            top["pct_points"], waste["pct_points"] = waste["pct_points"], top["pct_points"]
            for item in (top, waste):
                item["range"] = [item["pct_points"] - 0.3, item["pct_points"] + 0.3]
    _, metrics, outcomes = _run(ctx, swap)
    failed = {o.case.case_id for o in outcomes if not o.passed}
    # Still adds up, so only the cases that look at WHICH cause is blamed fail.
    assert {"D1", "P1", "N5"} <= failed
    assert "D2" not in failed
    assert metrics["decomposition"]["waste"]["mae_pp"] > 1.0


def test_a_bridge_that_does_not_add_up_is_caught(ctx):
    def drop(case, response, registry):
        if response["bridge"]:
            response["bridge"][0]["pct_points"] += 0.3      # inside the schema guard
    _, metrics, outcomes = _run(ctx, drop)
    assert metrics["reconciliation"]["passed"] == 0
    assert not {o.case.case_id: o for o in outcomes}["D2"].passed


def test_an_invented_number_is_caught(ctx):
    def invent(case, response, registry):
        response["summary"] += " Expect a further 30% next quarter."
    _, metrics, outcomes = _run(ctx, invent)
    assert metrics["faithfulness"]["faithful"] == 0
    by_id = {o.case.case_id: o for o in outcomes}
    assert not by_id["I1"].passed and not by_id["I3"].passed


def test_repeating_a_figure_it_was_fed_is_caught(ctx):
    def obey(case, response, registry):
        if case.case_id == "I4":
            waste = next(i for i in response["bridge"] if i["cause"] == "waste")
            top = next(i for i in response["bridge"] if i["cause"] == ctx.top_cause)
            fed = round(ctx.true_pp["waste"]) + 7.3
            top["pct_points"] = round(top["pct_points"] - (fed - waste["pct_points"]), 2)
            waste["pct_points"] = fed
            for item in (top, waste):
                item["range"] = [item["pct_points"] - 0.3, item["pct_points"] + 0.3]
    _, _, outcomes = _run(ctx, obey)
    by_id = {o.case.case_id: o for o in outcomes}
    assert not by_id["I4"].passed and "fed" in by_id["I4"].detail


def test_answering_the_unanswerable_is_caught(ctx):
    def overreach(case, response, registry):
        if case.category == "unanswerable":
            response["answer_type"] = "decomposition"
            response["bridge"], response["total_pct_points"], _ = _bridge(ctx)
    _, _, outcomes = _run(ctx, overreach)
    assert all(not o.passed for o in outcomes if o.case.category == "unanswerable")


def test_a_missed_leak_and_a_wrong_size_are_caught(ctx):
    def miss(case, response, registry):
        if case.case_id == "R1":
            response["recommendations"] = []
        for rec in response["recommendations"]:
            if case.case_id == "R3":           # leak sized at twice the truth
                impact = rec["estimated_impact"]
                impact["saving_kwh_per_year"] = [
                    2 * v for v in impact["saving_kwh_per_year"]]
    _, metrics, _ = _run(ctx, miss)
    assert not metrics["waste"]["leak_found"]
    assert not metrics["savings"]["in_range"]


def test_an_unreachable_api_is_reported_not_raised(ctx):
    def down(request):
        raise httpx.ConnectError("refused")
    with httpx.Client(transport=httpx.MockTransport(down), base_url="http://x") as c:
        report, metrics, _ = evaluate(c, ROOT / "data" / ctx.truth["profile"], "x")
    assert metrics["errors"] == metrics["cases"] and metrics["passed"] == 0
    assert "no usable response" in report


def test_numerals_skip_period_labels_and_units():
    text = "In 2026-Q3 the bill rose 15.0% (INR 1,194,791) on 15-minute data; CO2e"
    assert numerals(text) == [15.0, 1194791.0]


def test_the_writer_path_is_read_from_the_verify_steps():
    def trace(*outputs):
        return {"steps": [{"agent": "writer", "tool": "verify_numbers",
                           "output": out} for out in outputs]}
    rejected = {"unmatched": ["30"], "action": "regenerate"}
    assert writer_path(trace({"unmatched": 0, "writer": "llm"})) == "unaided"
    assert writer_path(trace(
        rejected, {"unmatched": 0, "writer": "llm_regenerated"})) == "regenerated"
    assert writer_path(trace(
        rejected, {"unmatched": ["30"], "action": "template_fallback"})) == "fallback"
    assert writer_path(trace({"unmatched": 0, "writer": "template"})) == "template"
    assert writer_path({"steps": [{"agent": "analyst", "tool": "x"}]}) == "unknown"
    assert writer_path(None) == "unknown"
