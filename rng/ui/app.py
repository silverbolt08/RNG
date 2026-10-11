"""Streamlit front end. Owner: teammate A.

    streamlit run rng/ui/app.py

Talks to the API over HTTP and imports nothing from the analysis: the demo
exercises exactly the path the judges test (CLAUDE.md section 4). Every number
on the page comes out of an /analyze or /trace response; the page computes
nothing of its own beyond laying the bridge out as a chart.
"""

from __future__ import annotations

import os

import httpx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")
TIMEOUT_S = 120.0

EXAMPLES = (
    "Why did the bill go up?",
    "Which assets use the most energy?",
    "What should we fix first?",
    "What if the tariff had been 10% lower?",
    "What will the tariff be next year?",
)

# External causes first, controllable waste last: the order of attribution.
CAUSE_ORDER = ("tariff", "volume", "product_mix", "weather", "waste")
CAUSE_LABEL = {
    "tariff": "Tariff (price)",
    "volume": "Production volume",
    "product_mix": "Product mix",
    "weather": "Weather",
    "waste": "Controllable waste",
}
ANSWER_LABEL = {
    "decomposition": "Bill decomposition",
    "recommendation": "Recommended actions",
    "calculated": "Calculated from the data",
    "not_in_data": "Not in the data",
    "clarification_needed": "Clarification needed",
    "refused": "Refused",
}
UP, DOWN, TOTAL = "#C0392B", "#1E8449", "#34495E"


# --------------------------------------------------------------------------
# API calls
# --------------------------------------------------------------------------


def api_get(path: str) -> dict:
    reply = httpx.get(f"{API_BASE_URL}{path}", timeout=TIMEOUT_S)
    reply.raise_for_status()
    return reply.json()


def api_post(path: str, body: dict) -> dict:
    reply = httpx.post(f"{API_BASE_URL}{path}", json=body, timeout=TIMEOUT_S)
    reply.raise_for_status()
    return reply.json()


def analyze(question: str, period: str, compare_to: str) -> dict:
    body = {"question": question.strip()}
    if period.strip():
        body["period"] = period.strip()
    if compare_to.strip():
        body["compare_to"] = compare_to.strip()
    return api_post("/analyze", body)


# --------------------------------------------------------------------------
# Laying a response out
# --------------------------------------------------------------------------


def ordered_bridge(bridge: list[dict]) -> list[dict]:
    rank = {cause: i for i, cause in enumerate(CAUSE_ORDER)}
    return sorted(bridge, key=lambda item: rank.get(item["cause"], len(rank)))


def waterfall(bridge: list[dict], total: float | None) -> go.Figure:
    """The one chart: five causes stepping up to the total change."""
    items = ordered_bridge(bridge)
    labels = [CAUSE_LABEL.get(i["cause"], i["cause"]) for i in items]
    values = [float(i["pct_points"]) for i in items]
    shown_total = sum(values) if total is None else float(total)
    hover = []
    for item in items:
        low, high = item["range"]
        line = f"{item['pct_points']:+.2f} pp (range {low:+.2f} to {high:+.2f})"
        if item.get("amount") is not None:
            line += f"<br>{item['amount']:+,.0f} in money"
        hover.append(line)
    fig = go.Figure(go.Waterfall(
        x=[*labels, "Total change"],
        y=[*values, shown_total],
        measure=["relative"] * len(values) + ["total"],
        text=[f"{v:+.1f} pp" for v in values] + [f"{shown_total:+.1f}%"],
        textposition="outside",
        hovertext=[*hover, f"{shown_total:+.2f}% change in the bill"],
        hoverinfo="x+text",
        increasing={"marker": {"color": UP}},
        decreasing={"marker": {"color": DOWN}},
        totals={"marker": {"color": TOTAL}},
        connector={"line": {"color": "#95A5A6", "width": 1}},
    ))
    fig.update_layout(
        title=f"What moved the bill: {shown_total:+.1f}% in total",
        yaxis_title="Change in the bill (percentage points)",
        showlegend=False, height=440,
        margin={"l": 50, "r": 20, "t": 60, "b": 40},
    )
    return fig


def bridge_table(bridge: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Cause": CAUSE_LABEL.get(i["cause"], i["cause"]),
        "Percentage points": i["pct_points"],
        "Low": i["range"][0],
        "High": i["range"][1],
        "Amount": i.get("amount"),
    } for i in ordered_bridge(bridge)])


def _span(low_high, digits: int = 0) -> str:
    low, high = low_high
    return f"{low:,.{digits}f} to {high:,.{digits}f}"


def recommendations_table(recommendations: list[dict]) -> pd.DataFrame:
    rows = []
    for rank, rec in enumerate(recommendations, start=1):
        impact = rec["estimated_impact"]
        rows.append({
            "#": rank,
            "Action": rec["action"],
            "Saving kWh / year": _span(impact["saving_kwh_per_year"]),
            "Saving money / year": _span(impact["saving_money_per_year"]),
            "CO2e tonnes / year": _span(impact["saving_co2e_t_per_year"], 1),
            "Payback months": _span(impact["payback_months"], 1),
            "Ease": impact["ease"],
            "Confidence": rec["confidence"],
            "Needs approval": "yes" if rec.get("requires_approval", True) else "no",
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Page sections
# --------------------------------------------------------------------------


def show_health() -> None:
    st.sidebar.header("System")
    st.sidebar.caption(f"API: {API_BASE_URL}")
    try:
        health = api_get("/health")
    except (httpx.HTTPError, ValueError) as exc:
        st.sidebar.error(f"API not reachable: {type(exc).__name__}")
        return
    for name, key in (("API", "api"), ("Data store", "data_store"), ("LLM", "llm")):
        part = health.get(key) or {}
        detail = f" ({part['detail']})" if part.get("detail") else ""
        st.sidebar.write(f"**{name}:** {part.get('status', 'unknown')}{detail}")
    if health.get("dataset_id"):
        st.sidebar.write(f"**Dataset:** {health['dataset_id']}")
    if health.get("mock_llm"):
        st.sidebar.info("MOCK_LLM is on: answers are worded by a template, "
                        "not a model. The numbers are the same either way.")


def show_trace(run_id: str) -> None:
    with st.expander("How this answer was produced (trace)"):
        st.caption(f"Run `{run_id}`, from `GET {API_BASE_URL}/trace/{run_id}`")
        try:
            trace = api_get(f"/trace/{run_id}")
        except (httpx.HTTPError, ValueError) as exc:
            st.warning(f"Trace not available: {type(exc).__name__}")
            return
        a, b, c = st.columns(3)
        a.metric("LLM calls", trace.get("llm_calls", 0))
        b.metric("Tokens", trace.get("tokens", 0))
        latency = trace.get("latency_ms")
        c.metric("Latency", "n/a" if latency is None else f"{latency:,.0f} ms")
        if trace.get("steps"):
            st.write("**Steps**")
            st.dataframe(pd.DataFrame([{
                "Step": s.get("seq"), "Agent": s.get("agent"),
                "Tool": s.get("tool"), "ms": s.get("ms"),
                "Check": s.get("check"),
            } for s in trace["steps"]]), hide_index=True, width="stretch")
        if trace.get("registry"):
            st.write("**Number registry** - the only figures the writer was "
                     "allowed to use")
            st.dataframe(pd.DataFrame(trace["registry"]), hide_index=True,
                         width="stretch")
        st.json(trace, expanded=False)


def show_answer(response: dict) -> None:
    kind = response.get("answer_type", "")
    st.subheader(ANSWER_LABEL.get(kind, kind or "Answer"))
    summary = response.get("summary") or "(no summary returned)"
    if kind in ("not_in_data", "clarification_needed", "refused"):
        st.warning(summary)
    else:
        st.write(summary)

    bridge = response.get("bridge") or []
    if bridge:
        st.plotly_chart(waterfall(bridge, response.get("total_pct_points")),
                        width="stretch")
        st.caption("Each bar is one cause, in percentage points of last "
                   "period's bill. The bars add up to the total. Hover for "
                   "the range.")
        st.dataframe(bridge_table(bridge), hide_index=True, width="stretch")

    recommendations = response.get("recommendations") or []
    if recommendations:
        st.subheader("Actions, fastest payback first")
        st.dataframe(recommendations_table(recommendations), hide_index=True,
                     width="stretch")
        st.caption("Every figure is a low-to-high range. Nothing here is "
                   "carried out automatically: each action needs approval.")
        for rank, rec in enumerate(recommendations, start=1):
            with st.expander(f"{rank}. {rec['action']} - why, and the evidence"):
                st.write(rec.get("rationale", ""))
                st.write("Evidence: " + ", ".join(
                    f"`{ref}`" for ref in rec.get("evidence_refs", [])))

    for title, key in (("Assumptions", "assumptions"), ("Limitations", "limitations")):
        if response.get(key):
            with st.expander(f"{title} ({len(response[key])})"):
                for line in response[key]:
                    st.write(f"- {line}")

    if response.get("run_id"):
        show_trace(response["run_id"])


def main() -> None:
    st.set_page_config(page_title="Factory energy cost", layout="wide")
    st.title("Why did the electricity bill change?")
    st.caption("Splits the change into tariff, volume, product mix, weather "
               "and controllable waste, then costs the fixes. Every number "
               "is computed from the meter data, never written by the model.")
    show_health()

    st.session_state.setdefault("question", EXAMPLES[0])
    st.write("Try one:")
    for column, example in zip(st.columns(len(EXAMPLES)), EXAMPLES, strict=True):
        if column.button(example, width="stretch"):
            st.session_state["question"] = example

    with st.form("ask"):
        question = st.text_area("Question", key="question", height=80)
        left, right = st.columns(2)
        period = left.text_input("Period (optional)", placeholder="YYYY-Qn")
        compare_to = right.text_input("Compare to (optional)", placeholder="YYYY-Qn")
        asked = st.form_submit_button("Ask", type="primary")

    if asked:
        if not question.strip():
            st.error("Type a question first.")
        else:
            try:
                with st.spinner("Working it out..."):
                    st.session_state["response"] = analyze(question, period, compare_to)
            except httpx.HTTPStatusError as exc:
                st.session_state.pop("response", None)
                st.error(f"The API answered {exc.response.status_code}: "
                         f"{exc.response.text[:500]}")
            except (httpx.HTTPError, ValueError) as exc:
                st.session_state.pop("response", None)
                st.error(f"Could not reach the API at {API_BASE_URL}: "
                         f"{type(exc).__name__}")

    if "response" in st.session_state:
        st.divider()
        show_answer(st.session_state["response"])


if __name__ == "__main__":
    main()
