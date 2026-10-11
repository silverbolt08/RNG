"""The state that flows through the graph. One request, one dict."""

from __future__ import annotations

from typing import Any, TypedDict


class GraphState(TypedDict, total=False):
    # request
    run_id: str
    question: str
    period: str | None
    compare_to: str | None

    # classify + guard
    intent: dict[str, Any]
    kind: str                    # which deterministic handler runs
    answer_type: str
    stop_reason: str | None      # set when the guard ends the run early
    periods: list[str]
    assets: list[str]

    # handler output: numbers live in the registry, nowhere else
    registry: Any                # rng.graph.registry.Registry
    facts: dict[str, Any]        # ordered slots for the template writer
    notes: list[str]             # deterministic hints for the LLM writer
    bridge: list[Any]
    recommendations: list[Any]
    assumptions: list[str]
    limitations: list[str]
    total_pct_points: float | None

    # writer + verify
    summary: str
    rejected: list[str]          # numerals the check refused
    attempts: int
    writer: str                  # "llm" | "llm_regenerated" | "template"

    # trace
    steps: list[dict[str, Any]]
    llm_calls: int
    tokens: int
