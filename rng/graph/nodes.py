"""The seven nodes. Two call an LLM, at the two ends; five are plain code.

    classify -> guard -> load_analysis -> analyse -> write_brief -> verify -> respond
     LLM #1                                           LLM #2

The LLM classifies the request and writes the prose. It never computes a
number and never decides what is wasteful: by the time write_brief runs, every
figure already sits in the registry, and verify refuses any numeral that is
not there.
"""

from __future__ import annotations

import time
from typing import Any

from rng.config import assumptions
from rng.graph import brief
from rng.graph.handlers import HANDLERS, PAIR_KINDS
from rng.graph.llm import LLM
from rng.graph.registry import Registry
from rng.graph.store_io import Store
from rng.periods import period_bounds

ANSWER_TYPE = {"bridge": "decomposition", "bill_total": "calculated",
               "top_users": "calculated", "asset_change": "calculated", "idle_load": "calculated",
               "what_if": "calculated", "savings": "recommendation",
               "ranking": "recommendation"}
MAX_LLM_DRAFTS = 2          # one draft, one regeneration, then the template


def _step(state: dict, agent: str, tool: str, started: float, *,
          input: Any = None, output: Any = None, **extra: Any) -> list[dict]:
    step = {"agent": agent, "tool": tool, "input": input, "output": output,
            "ms": round((time.perf_counter() - started) * 1000, 2), **extra}
    return [*state.get("steps", []), step]


def make_nodes(store: Store, llm: LLM) -> dict[str, Any]:

    # 1 -- LLM call one ---------------------------------------------------
    def classify(state: dict) -> dict:
        t = time.perf_counter()
        periods, assets = store.periods(), store.assets()
        intent, usage = llm.classify(state["question"], periods, assets)
        return {"intent": intent.as_dict(), "periods": periods, "assets": assets,
                "llm_calls": state.get("llm_calls", 0) + usage["calls"],
                "tokens": state.get("tokens", 0) + usage["tokens"],
                "steps": _step(state, "classifier", "classify_request", t,
                               input={"question": state["question"]},
                               output=intent.as_dict())}

    # 2 -- is it answerable at all? ---------------------------------------
    def guard(state: dict) -> dict:
        t = time.perf_counter()
        intent, periods = state["intent"], state["periods"]
        kind = intent["intent"]

        def stop(answer_type: str, reason: str) -> dict:
            return {"answer_type": answer_type, "stop_reason": reason, "kind": kind,
                    "steps": _step(state, "analyst", "guard", t,
                                   input={"intent": kind},
                                   output={"answer_type": answer_type,
                                           "reason": reason})}

        if kind == "refuse":
            return stop("refused", intent.get("reason") or
                        "it asks for a figure not computed from the data")
        if kind == "out_of_scope":
            return stop("not_in_data", intent.get("reason") or
                        "it is outside the loaded tables")
        if kind == "unclear" or kind not in HANDLERS:
            return stop("clarification_needed", intent.get("reason") or
                        "what would you like analysed")
        if not periods:
            return stop("not_in_data", "no data has been loaded")

        period = state.get("period") or intent.get("period") or periods[-1]
        compare_to = state.get("compare_to") or intent.get("compare_to")
        for label in filter(None, (period, compare_to)):
            try:
                period_bounds(label)
            except ValueError:
                return stop("clarification_needed",
                            f"'{label}' is not a period I can read (use a "
                            "label such as the ones listed)")
            if label not in periods:
                return stop("not_in_data", f"there is no data for {label}")
        notes = []
        if not (state.get("period") or intent.get("period")):
            notes.append(f"No period was given, so the latest one in the data "
                         f"({period}) was used.")
        if kind in PAIR_KINDS:
            if not compare_to:
                earlier = [p for p in periods if p < period]
                if not earlier:
                    return stop("not_in_data",
                                f"there is no earlier period to compare {period} with")
                compare_to = earlier[-1]
                notes.append(f"No comparison period was given, so the one "
                             f"before it ({compare_to}) was used.")
            if compare_to == period:
                return stop("clarification_needed",
                            "the period and the comparison period are the same")
        else:
            compare_to = None
        return {"kind": kind, "answer_type": ANSWER_TYPE[kind], "period": period,
                "compare_to": compare_to, "stop_reason": None,
                "assumptions": notes,
                "steps": _step(state, "analyst", "guard", t, input={"intent": kind},
                               output={"period": period, "compare_to": compare_to,
                                       "in_scope": True})}

    # 3 -- cache read ------------------------------------------------------
    def load_analysis(state: dict) -> dict:
        t = time.perf_counter()
        dataset = store.dataset_id()
        return {"steps": _step(state, "analyst", "load_analysis", t,
                               input={"period": state["period"],
                                      "compare_to": state.get("compare_to")},
                               output={"dataset_id": dataset,
                                       "served_from": "analysis_cache"})}

    # 4 -- the deterministic handler --------------------------------------
    def analyse(state: dict) -> dict:
        t = time.perf_counter()
        result = HANDLERS[state["kind"]](store, state)
        if "missing" in result:
            return {"answer_type": "not_in_data", "stop_reason": result["missing"],
                    "steps": _step(state, "analyst", state["kind"], t,
                                   output={"available": False,
                                           "reason": result["missing"]})}
        steps = state.get("steps", [])
        for tool, inp, out in result.pop("trace"):
            steps = [*steps, {"agent": "analyst", "tool": tool, "input": inp,
                              "output": out,
                              "ms": round((time.perf_counter() - t) * 1000, 2)}]
        update = {"bridge": [], "recommendations": [], "total_pct_points": None,
                  **result, "steps": steps, "attempts": 0, "rejected": []}
        update["limitations"] = result.get("limitations", [])
        return update

    # 5 -- LLM call two ----------------------------------------------------
    def write_brief(state: dict) -> dict:
        t = time.perf_counter()
        reg: Registry = state["registry"]
        attempts = state.get("attempts", 0)
        prose, usage = llm.write(state["question"], reg.facts_table(),
                                 "\n".join(state.get("notes", [])),
                                 rejected=state.get("rejected") or None)
        if prose:
            writer = "llm" if attempts == 0 else "llm_regenerated"
        else:
            prose, writer = brief.render(state["kind"], reg, state), "template"
        return {"summary": prose, "writer": writer, "attempts": attempts + 1,
                "llm_calls": state.get("llm_calls", 0) + usage["calls"],
                "tokens": state.get("tokens", 0) + usage["tokens"],
                "steps": _step(state, "writer", "write_finance_brief", t,
                               input={"facts": len(reg), "attempt": attempts + 1},
                               output={"writer": writer, "chars": len(prose)})}

    # 6 -- numbers against the registry -----------------------------------
    def verify(state: dict) -> dict:
        t = time.perf_counter()
        reg: Registry = state["registry"]
        bad = reg.unmatched(state["summary"])
        writer = state["writer"]
        if not bad:
            return {"rejected": [],
                    "steps": _step(state, "writer", "verify_numbers", t,
                                   output={"numerals": len(reg.numerals(state["summary"])),
                                           "unmatched": 0, "writer": writer},
                                   check="all numbers match tool outputs")}
        if writer.startswith("llm") and state["attempts"] < MAX_LLM_DRAFTS:
            return {"rejected": bad,          # routes back to write_brief
                    "steps": _step(state, "writer", "verify_numbers", t,
                                   output={"unmatched": bad, "action": "regenerate"},
                                   check=f"rejected: {', '.join(bad)}")}
        summary, final = brief.render(state["kind"], reg, state), "template_fallback"
        if reg.unmatched(summary):            # cannot happen; never ship it if it does
            summary, final = brief.SAFE, "withheld"
        return {"summary": summary, "writer": final, "rejected": [],
                "limitations": [*state.get("limitations", []),
                                "The LLM's draft quoted a number that no tool "
                                "produced, so the template brief was used."],
                "steps": _step(state, "writer", "verify_numbers", t,
                               output={"unmatched": bad, "action": final},
                               check=f"rejected: {', '.join(bad)}; fell back to template")}

    # 7 -- assemble --------------------------------------------------------
    def respond(state: dict) -> dict:
        if state.get("stop_reason"):
            return {"summary": brief.stop_message(
                        state["answer_type"], state["stop_reason"],
                        state.get("periods", []), state.get("assets", [])),
                    "bridge": [], "recommendations": [], "total_pct_points": None,
                    "assumptions": [], "writer": "fixed_message",
                    "limitations": ["No analysis was run for this request."]}
        notes = state.get("assumptions", [])
        limits = list(state.get("limitations", []))
        if state.get("writer") == "template" and llm.mock:
            limits.append("MOCK_LLM is on: the brief was written by the offline template.")
        return {"assumptions": [*notes, *assumptions().as_statements()],
                "limitations": limits}

    return {"classify": classify, "guard": guard, "load_analysis": load_analysis,
            "analyse": analyse, "write_brief": write_brief, "verify": verify,
            "respond": respond}
