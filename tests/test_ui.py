"""The Streamlit page, run headless against a stand-in for the API."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from rng.api.schemas import AnalyzeResponse
from rng.ui.app import bridge_table, recommendations_table, waterfall

APP = str(Path(__file__).resolve().parents[1] / "rng" / "ui" / "app.py")

BRIDGE = [
    {"cause": "waste", "pct_points": 1.5, "range": [0.9, 2.1], "amount": 120.0},
    {"cause": "tariff", "pct_points": 7.4, "range": [7.0, 7.8], "amount": 590.0},
    {"cause": "product_mix", "pct_points": 2.9, "range": [2.2, 3.5], "amount": 230.0},
    {"cause": "volume", "pct_points": 1.6, "range": [1.1, 2.0], "amount": 130.0},
    {"cause": "weather", "pct_points": 1.6, "range": [1.0, 2.3], "amount": 130.0},
]
RECOMMENDATION = {
    "action": "Find and repair the continuous loss on compressors",
    "rationale": "Night load stepped up.",
    "evidence_refs": ["meter_clean:compressors"],
    "confidence": "high",
    "estimated_impact": {
        "saving_kwh_per_year": [78840, 111690],
        "saving_money_per_year": [394200, 558450],
        "saving_co2e_t_per_year": [55.98, 79.3],
        "payback_months": [1.0, 2.7],
        "ease": "easy",
    },
    "requires_approval": True,
}
RESPONSE = AnalyzeResponse.model_validate({
    "run_id": "run-1", "answer_type": "decomposition",
    "summary": "The bill rose 15.0%, mostly on price.",
    "bridge": BRIDGE, "recommendations": [RECOMMENDATION],
    "assumptions": ["Emission factor 0.71."], "limitations": ["One site."],
    "total_pct_points": 15.0,
}).model_dump(mode="json")
TRACE = {"run_id": "run-1", "question": "q", "llm_calls": 2, "tokens": 310,
         "latency_ms": 41.0,
         "steps": [{"seq": 1, "agent": "analyst", "tool": "decompose_change"}],
         "registry": [{"key": "tariff_pp", "value": 7.4, "unit": "pp",
                       "source_tool": "decompose_change", "evidence_ref": "t"}]}
HEALTH = {"status": "ok", "api": {"status": "ok"}, "data_store": {"status": "ok"},
          "llm": {"status": "ok", "detail": "mock"}, "dataset_id": "d1",
          "mock_llm": True}


def _reply(url: str, payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("GET", url))


def _get(url, **_):
    return _reply(url, TRACE if "/trace/" in url else HEALTH)


def test_the_waterfall_steps_through_the_causes_to_the_total():
    fig = waterfall(BRIDGE, 15.0)
    bars = fig.data[0]
    # External causes first, controllable waste last, then the total.
    assert list(bars.x) == ["Tariff (price)", "Production volume", "Product mix",
                            "Weather", "Controllable waste", "Total change"]
    assert list(bars.measure) == ["relative"] * 5 + ["total"]
    assert list(bars.y) == [7.4, 1.6, 2.9, 1.6, 1.5, 15.0]
    assert "+15.0%" in fig.layout.title.text


def test_tables_show_ranges_not_point_estimates():
    assert list(bridge_table(BRIDGE)["Low"]) == [7.0, 1.1, 2.2, 1.0, 0.9]
    row = recommendations_table([RECOMMENDATION]).iloc[0]
    assert row["Saving kWh / year"] == "78,840 to 111,690"
    assert row["Payback months"] == "1.0 to 2.7"
    assert row["Needs approval"] == "yes"


def test_the_page_asks_the_api_and_shows_the_answer():
    sent = {}

    def post(url, json=None, **_):
        sent.update(url=url, body=json)
        return _reply(url, RESPONSE)

    with patch("httpx.get", _get), patch("httpx.post", post):
        at = AppTest.from_file(APP, default_timeout=30).run()
        assert not at.exception
        at.text_input[0].set_value("2026-Q3")
        at.button[-1].click().run()

    assert not at.exception
    assert sent["url"].endswith("/analyze")
    assert sent["body"] == {"question": "Why did the bill go up?", "period": "2026-Q3"}
    page = " ".join(str(m.value) for m in at.markdown)
    assert "The bill rose 15.0%, mostly on price." in page
    assert [s.value for s in at.subheader] == [
        "Bill decomposition", "Actions, fastest payback first"]
    assert len(at.get("plotly_chart")) == 1
    assert {m.label: m.value for m in at.metric}["LLM calls"] == "2"


def test_an_unreachable_api_shows_an_error_instead_of_crashing():
    def refuse(url, **_):
        raise httpx.ConnectError("refused")

    with patch("httpx.get", refuse), patch("httpx.post", refuse):
        at = AppTest.from_file(APP, default_timeout=30).run()
        at.button[-1].click().run()
    assert not at.exception
    assert any("Could not reach the API" in e.value for e in at.error)


@pytest.mark.parametrize("kind", ["not_in_data", "clarification_needed", "refused"])
def test_a_declined_answer_is_shown_as_a_warning_with_no_chart(kind):
    declined = {"run_id": "r2", "answer_type": kind,
                "summary": "The loaded data cannot answer that.", "bridge": [],
                "recommendations": [], "assumptions": [], "limitations": []}
    with patch("httpx.get", _get), \
            patch("httpx.post", lambda url, **_: _reply(url, declined)):
        at = AppTest.from_file(APP, default_timeout=30).run()
        at.button[-1].click().run()
    assert not at.exception
    assert any("cannot answer" in w.value for w in at.warning)
    assert len(at.get("plotly_chart")) == 0
