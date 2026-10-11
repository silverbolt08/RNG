"""The LangGraph wiring, and the one entry point the API calls.

A fixed graph, not an agent loop. There are a known number of question types,
so the LLM picks one and deterministic code does the rest: latency is bounded,
the call count is at most three (classify, write, one regeneration), and the
trace reads top to bottom.
"""

from __future__ import annotations

import time
import uuid

from langgraph.graph import END, START, StateGraph

from rng.api.schemas import AnalyzeRequest, AnalyzeResponse
from rng.graph.llm import LLM
from rng.graph.nodes import make_nodes
from rng.graph.state import GraphState
from rng.graph.store_io import Store


def build_graph(store: Store, llm: LLM):
    nodes = make_nodes(store, llm)
    g = StateGraph(GraphState)
    for name, fn in nodes.items():
        g.add_node(name, fn)
    g.add_edge(START, "classify")
    g.add_edge("classify", "guard")
    g.add_conditional_edges(
        "guard", lambda s: "stop" if s.get("stop_reason") else "go",
        {"stop": "respond", "go": "load_analysis"})
    g.add_edge("load_analysis", "analyse")
    g.add_conditional_edges(
        "analyse", lambda s: "stop" if s.get("stop_reason") else "go",
        {"stop": "respond", "go": "write_brief"})
    g.add_edge("write_brief", "verify")
    g.add_conditional_edges(
        "verify", lambda s: "regenerate" if s.get("rejected") else "done",
        {"regenerate": "write_brief", "done": "respond"})
    g.add_edge("respond", END)
    return g.compile()


class Engine:
    """Holds the compiled graph. One per process."""

    def __init__(self, store: Store, llm: LLM):
        self.store, self.llm = store, llm
        self._graph = build_graph(store, llm)

    def answer(self, request: AnalyzeRequest) -> AnalyzeResponse:
        started = time.perf_counter()
        run_id = f"en-{uuid.uuid4().hex[:6]}"
        state = self._graph.invoke({
            "run_id": run_id, "question": request.question,
            "period": request.period, "compare_to": request.compare_to,
            "steps": [], "llm_calls": 0, "tokens": 0})
        response = AnalyzeResponse(
            run_id=run_id, answer_type=state["answer_type"],
            summary=state["summary"], bridge=state.get("bridge", []),
            recommendations=state.get("recommendations", []),
            assumptions=state.get("assumptions", []),
            limitations=state.get("limitations", []),
            total_pct_points=state.get("total_pct_points"))
        registry = state.get("registry")
        self.store.save_run(
            run_id=run_id, question=request.question,
            answer_type=response.answer_type.value,
            request=request.model_dump(mode="json"),
            response=response.model_dump(mode="json"),
            llm_calls=state.get("llm_calls", 0), tokens=state.get("tokens", 0),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            steps=state.get("steps", []),
            registry=registry.rows() if registry is not None else [])
        return response
