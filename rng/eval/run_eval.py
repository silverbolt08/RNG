"""Run the evaluation set against the API and write the report. Owner: teammate A.

    python -m rng.eval.run_eval [--api http://localhost:8000]
                                [--data-dir data/primary]
                                [--out docs/eval_report.md]

Every case is posted to POST /analyze over HTTP -- the path the judges test --
and its GET /trace/{run_id} record is fetched. Answers are compared with the
generator's ground_truth.json and with facts measured on the raw CSVs.

Reported (guide section 10): decomposition error per cause, total
reconciliation pass rate, waste detection, savings accuracy, number
faithfulness (unaided pass rate, regenerations, fallbacks), latency p50/p95,
LLM calls and tokens.

The API must be serving the same dataset as --data-dir; the report says so
loudly when /health disagrees.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np

from rng.config import settings
from rng.eval.cases import (
    ADDITIVITY_TOL_PP,
    CAUSE_TOL_PP,
    CAUSES,
    MEASURED_TOL,
    MINIMUMS,
    TOTAL_TOL_PP,
    Case,
    Context,
    bridge_pp,
    build_cases,
    load_context,
    unbacked_numbers,
)

TIMEOUT_S = 120.0


@dataclass
class Outcome:
    case: Case
    response: dict
    trace: dict | None
    answer_type: str | None
    type_ok: bool
    check_ok: bool
    detail: str
    latency_ms: float
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.type_ok and self.check_ok


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


def run_case(client: httpx.Client, case: Case, ctx: Context) -> Outcome:
    started = time.perf_counter()
    try:
        reply = client.post("/analyze", json=case.request())
        latency = 1000.0 * (time.perf_counter() - started)
        reply.raise_for_status()
        response = reply.json()
    except (httpx.HTTPError, ValueError) as exc:
        latency = 1000.0 * (time.perf_counter() - started)
        return Outcome(case, {}, None, None, False, False,
                       "no usable response", latency, f"{type(exc).__name__}: {exc}")

    trace = None
    run_id = response.get("run_id")
    if run_id:
        try:
            got = client.get(f"/trace/{run_id}")
            if got.status_code == 200:
                trace = got.json()
        except (httpx.HTTPError, ValueError):
            trace = None

    answer_type = response.get("answer_type")
    type_ok = answer_type in case.expected
    try:
        check_ok, detail = case.check(response, trace, ctx)
    except Exception as exc:                      # noqa: BLE001 -- a malformed
        check_ok, detail = False, f"check could not read the response: {exc!r}"
    if not type_ok:
        detail = (f"answer_type {answer_type!r}, expected "
                  f"{' or '.join(case.expected)}; {detail}")
    return Outcome(case, response, trace, answer_type, type_ok, check_ok,
                   detail, latency)


def run_cases(client: httpx.Client, cases: list[Case], ctx: Context) -> list[Outcome]:
    return [run_case(client, case, ctx) for case in cases]


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def writer_path(trace: dict | None) -> str:
    """How the brief got through the number check.

    unaided      the LLM's first draft matched the registry
    regenerated  its first draft was rejected, the second matched
    fallback     both drafts were rejected; the template brief was used
    template     no LLM draft at all (MOCK_LLM, or no key)
    unknown      the trace does not say (no brief was written)

    Read from the writer's verify_numbers steps: `action` on a rejected draft,
    `writer` on the one that passed.
    """
    path = "unknown"
    for step in (trace or {}).get("steps") or []:
        if "verify" not in str(step.get("tool", "")).lower():
            continue
        out = step.get("output") or {}
        action, writer = str(out.get("action", "")), str(out.get("writer", ""))
        if action in ("template_fallback", "withheld") or out.get("fallback"):
            return "fallback"
        if action == "regenerate" or writer == "llm_regenerated"                 or (out.get("attempts") or 1) > 1:
            path = "regenerated"
        elif writer == "template":
            path = "template"
        elif path == "unknown":
            path = "unaided"
    return path


def _is_base_bridge(outcome: Outcome) -> bool:
    """A full five-cause bridge for the base comparison, not a what-if."""
    return (outcome.case.category != "what_if"
            and set(CAUSES) <= set(bridge_pp(outcome.response)))


def summarise(outcomes: list[Outcome], ctx: Context) -> dict:
    by_id = {o.case.case_id: o for o in outcomes}
    answered = [o for o in outcomes if o.error is None]

    # Decomposition error: every base-case bridge the API returned.
    bridges = [bridge_pp(o.response) for o in outcomes if _is_base_bridge(o)]
    decomposition = {}
    for cause in CAUSES:
        reported = [b[cause] for b in bridges]
        decomposition[cause] = {
            "true_pp": ctx.true_pp[cause],
            "reported_pp": float(np.mean(reported)) if reported else None,
            "mae_pp": (float(np.mean([abs(r - ctx.true_pp[cause]) for r in reported]))
                       if reported else None),
        }
    headline = by_id.get("D1")
    ranges = {item["cause"]: item.get("range")
              for item in (headline.response.get("bridge") or [])} if headline else {}
    for cause in CAUSES:
        low_high = ranges.get(cause)
        decomposition[cause]["range"] = low_high
        decomposition[cause]["truth_in_range"] = (
            low_high[0] <= ctx.true_pp[cause] <= low_high[1] if low_high else None)

    # Reconciliation: every response that carries a bridge and a total.
    with_bridge = [o for o in answered if o.response.get("bridge")
                   and o.response.get("total_pct_points") is not None]
    reconciled = [o for o in with_bridge
                  if abs(sum(bridge_pp(o.response).values())
                         - float(o.response["total_pct_points"])) <= ADDITIVITY_TOL_PP]

    # Number faithfulness, re-checked from outside and read from the trace.
    traced = [o for o in answered if o.trace is not None]
    faithful = [o for o in traced if not unbacked_numbers(o.response, o.trace)]
    paths = Counter(writer_path(o.trace) for o in traced)
    known = paths["unaided"] + paths["regenerated"] + paths["fallback"]

    leak_case, saving_case = by_id.get("R1"), by_id.get("R3")
    leak_asset = ctx.leak["meter_id"] if ctx.leak else None
    recs = (leak_case.response.get("recommendations") or []) if leak_case else []
    elsewhere = [r.get("action", "?") for r in recs
                 if leak_asset and leak_asset.lower() not in json.dumps(r).lower()]

    latencies = [o.latency_ms for o in answered]
    server = [o.trace["latency_ms"] for o in traced
              if o.trace.get("latency_ms") is not None]
    llm_calls = [int(o.trace.get("llm_calls") or 0) for o in traced]
    tokens = [int(o.trace.get("tokens") or 0) for o in traced]
    categories = Counter(o.case.category for o in outcomes)

    def pct(values: list[float], q: float) -> float | None:
        return float(np.percentile(values, q)) if values else None

    return {
        "cases": len(outcomes),
        "passed": sum(o.passed for o in outcomes),
        "errors": sum(o.error is not None for o in outcomes),
        "answer_type_correct": sum(o.type_ok for o in outcomes),
        "by_category": {
            cat: {"cases": n,
                  "passed": sum(o.passed for o in outcomes if o.case.category == cat),
                  "minimum": MINIMUMS.get(cat)}
            for cat, n in categories.items()},
        "decomposition": decomposition,
        "bridges_scored": len(bridges),
        "total_rise": {"true": ctx.rise_pct,
                       "reported": (headline.response.get("total_pct_points")
                                    if headline else None)},
        "reconciliation": {"passed": len(reconciled), "of": len(with_bridge)},
        "waste": {"leak_asset": leak_asset,
                  "leak_found": bool(leak_case and leak_case.check_ok),
                  "detail": leak_case.detail if leak_case else "case not run",
                  "actions_on_other_assets": elsewhere,
                  "true_waste_pp": ctx.true_pp["waste"],
                  "reported_waste_pp": decomposition["waste"]["reported_pp"]},
        "savings": {"in_range": bool(saving_case and saving_case.check_ok),
                    "detail": saving_case.detail if saving_case else "case not run"},
        "faithfulness": {"traced": len(traced), "faithful": len(faithful),
                         "unaided": paths["unaided"],
                         "regenerated": paths["regenerated"],
                         "fallback": paths["fallback"],
                         "template": paths["template"],
                         "unknown": paths["unknown"], "known": known},
        "latency_ms": {"p50": pct(latencies, 50), "p95": pct(latencies, 95),
                       "server_p50": pct(server, 50), "server_p95": pct(server, 95)},
        "llm": {"calls_total": sum(llm_calls), "tokens_total": sum(tokens),
                "calls_per_request": float(np.mean(llm_calls)) if llm_calls else None,
                "calls_max": max(llm_calls, default=None),
                "tokens_per_request": float(np.mean(tokens)) if tokens else None},
    }


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def _n(value, digits: int = 2, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}{suffix}"


def _rate(part: int, whole: int) -> str:
    return "n/a (0 responses)" if not whole else f"{part}/{whole} ({100 * part / whole:.0f}%)"


def render_report(metrics: dict, outcomes: list[Outcome], ctx: Context,
                  meta: dict) -> str:
    m = metrics
    lines = [
        "# Evaluation report",
        "",
        f"- Run at: {meta['run_at']}",
        f"- API: `{meta['api']}`",
        f"- Dataset: `{meta['data_dir']}` (`{ctx.truth.get('dataset_id', '?')}`), "
        f"{ctx.period} against {ctx.compare_to}",
        f"- API reports dataset `{meta.get('api_dataset_id') or 'unknown'}`, "
        f"MOCK_LLM = {meta.get('mock_llm')}, health `{meta.get('health') or 'unknown'}`",
        "",
    ]
    if meta.get("health") != "ok":
        lines += [f"> **WARNING: the API reported health "
                  f"`{meta.get('health') or 'unknown'}` before this run. If its "
                  "data did not load, the results below describe a system "
                  "with nothing to analyse, not the analysis.**", ""]
    if meta.get("dataset_mismatch"):
        lines += ["> **WARNING: the API is not serving the dataset this truth "
                  "belongs to. Every comparison below is invalid.**", ""]
    if m["errors"]:
        lines += [f"> **{m['errors']} of {m['cases']} requests got no usable "
                  "response.** See the case table.", ""]

    f, lat, llm = m["faithfulness"], m["latency_ms"], m["llm"]
    lines += [
        "## Headline",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Cases passed | {_rate(m['passed'], m['cases'])} |",
        f"| Correct `answer_type` | {_rate(m['answer_type_correct'], m['cases'])} |",
        f"| Total change, reported vs measured | {_n(m['total_rise']['reported'])} "
        f"vs {_n(m['total_rise']['true'])} pp |",
        f"| Total reconciliation (five causes sum to the total, within "
        f"{ADDITIVITY_TOL_PP} pp) | {_rate(m['reconciliation']['passed'], m['reconciliation']['of'])} |",
        f"| Leak found on `{m['waste']['leak_asset']}` | "
        f"{'yes' if m['waste']['leak_found'] else 'NO'} |",
        f"| Actions recommended on other assets | {len(m['waste']['actions_on_other_assets'])} |",
        f"| True saving inside the stated range | "
        f"{'yes' if m['savings']['in_range'] else 'NO'} ({m['savings']['detail']}) |",
        f"| Number faithfulness, re-checked here | {_rate(f['faithful'], f['traced'])} |",
        f"| LLM draft passed the number check unaided | {_rate(f['unaided'], f['known'])} |",
        f"| Regenerated once / fell back to template | {f['regenerated']} / {f['fallback']} |",
        f"| Briefs written by the template with no LLM draft | {f['template']} |",
        f"| Latency p50 / p95, client side | {_n(lat['p50'], 0)} / {_n(lat['p95'], 0)} ms |",
        f"| Latency p50 / p95, server reported | {_n(lat['server_p50'], 0)} / "
        f"{_n(lat['server_p95'], 0)} ms |",
        f"| LLM calls per request (mean / max) | {_n(llm['calls_per_request'])} / "
        f"{llm['calls_max'] if llm['calls_max'] is not None else 'n/a'} |",
        f"| LLM calls / tokens, whole run | {llm['calls_total']} / {llm['tokens_total']:,} |",
        "",
        "## Decomposition error",
        "",
        f"Against the generator's true split, over {m['bridges_scored']} "
        "base-case bridge(s) returned. The truth is the same 16-subset "
        "Shapley split evaluated on the generator's physical model; the "
        "system fits a linear baseline from the CSVs and never sees it.",
        "",
        "| Cause | True pp | Reported pp | MAE pp | Reported range | Truth in range |",
        "|---|---|---|---|---|---|",
    ]
    for cause in CAUSES:
        d = m["decomposition"][cause]
        low_high = d["range"]
        inside = {True: "yes", False: "no", None: "n/a"}[d["truth_in_range"]]
        lines.append(
            f"| {cause} | {_n(d['true_pp'])} | {_n(d['reported_pp'])} | "
            f"{_n(d['mae_pp'])} | "
            f"{f'{low_high[0]:.2f} to {low_high[1]:.2f}' if low_high else 'n/a'} | {inside} |")
    maes = [m["decomposition"][c]["mae_pp"] for c in CAUSES
            if m["decomposition"][c]["mae_pp"] is not None]
    lines += ["", f"Mean absolute error across the five causes: "
                  f"{_n(float(np.mean(maes)) if maes else None)} pp.", ""]

    lines += ["## Waste detection", "",
              f"- Injected: a leak on `{m['waste']['leak_asset']}`. "
              f"Result: {m['waste']['detail']}.",
              f"- Waste term: reported {_n(m['waste']['reported_waste_pp'])} pp, "
              f"true {_n(m['waste']['true_waste_pp'])} pp."]
    if m["waste"]["actions_on_other_assets"]:
        lines.append("- Recommended on assets with no injected fault: "
                     + "; ".join(m["waste"]["actions_on_other_assets"])
                     + ". Idle-load actions are legitimate; a leak repair "
                     "here would be a false positive.")
    else:
        lines.append("- No action was recommended on any other asset.")
    lines.append("")

    lines += ["## Cases by category", "",
              "| Category | Cases | Required | Passed |", "|---|---|---|---|"]
    for cat, row in m["by_category"].items():
        need = row["minimum"] if row["minimum"] is not None else "-"
        lines.append(f"| {cat} | {row['cases']} | {need} | {row['passed']} |")
    lines.append("")

    lines += ["## Every case", "",
              "| ID | Category | Question | Expected | Got | Result | Detail | ms |",
              "|---|---|---|---|---|---|---|---|"]
    for o in outcomes:
        detail = (o.error or o.detail).replace("|", "/").replace("\n", " ")
        lines.append(
            f"| {o.case.case_id} | {o.case.category} | {o.case.question} | "
            f"{' / '.join(o.case.expected)} | {o.answer_type or '-'} | "
            f"{'PASS' if o.passed else 'FAIL'} | {detail} | {o.latency_ms:.0f} |")
    lines.append("")

    lines += [
        "## How to read this",
        "",
        f"- A cause passes within {CAUSE_TOL_PP} pp of truth; the total within "
        f"{TOTAL_TOL_PP} pp, because it is measured, not modelled.",
        f"- Measured checks (asset totals) allow {MEASURED_TOL:.0%}: the "
        "evaluation sums the raw rows, the system cleans them first.",
        "- Savings accuracy: the injected leak's kW over 8,760 hours, at the "
        "middle of the declared recovery range, must fall inside the "
        "recommended kWh range.",
        "- Number faithfulness is re-checked here from outside: every figure "
        "in `summary` must match a registry row from `/trace` within 0.05 "
        "(0.5% for large figures). The unaided / regenerated / fallback "
        "split is read from the `verify_numbers` steps of the trace and "
        "counts LLM drafts only.",
        "- Latency is wall-clock time for `POST /analyze` from this script; "
        "the server figure is `latency_ms` from the trace.",
        "",
        "## Known limits",
        "",
        "- One dataset per run. Weather and waste carry an expected error: "
        "the generator's cooling response is piecewise and the baseline is "
        "linear, so part of the weather effect lands in the residual.",
        "- The truth is defined by the same counterfactual construction the "
        "system uses (it has to be, or the comparison means nothing), but "
        "with the physical model in place of the fitted one.",
        "- With `MOCK_LLM=true` every brief is written by the template, so "
        "there are no LLM drafts and the unaided pass rate is not measured.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def evaluate(client: httpx.Client, data_dir: str | Path,
             api_label: str) -> tuple[str, dict, list[Outcome]]:
    """Run every case through `client`; return (report, metrics, outcomes)."""
    ctx = load_context(data_dir)
    meta = {"run_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            "api": api_label, "data_dir": Path(data_dir).as_posix()}
    try:
        health = client.get("/health").json()
        meta.update(health=health.get("status"), mock_llm=health.get("mock_llm"),
                    api_dataset_id=health.get("dataset_id"))
        truth_id = ctx.truth.get("dataset_id")
        meta["dataset_mismatch"] = bool(
            truth_id and health.get("dataset_id") and health["dataset_id"] != truth_id)
    except (httpx.HTTPError, ValueError):
        meta["health"] = "unreachable"
    outcomes = run_cases(client, build_cases(ctx), ctx)
    metrics = summarise(outcomes, ctx)
    return render_report(metrics, outcomes, ctx, meta), metrics, outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the evaluation set.")
    parser.add_argument("--api", default=settings().api_base_url)
    parser.add_argument("--data-dir", type=Path, default=settings().data_dir)
    parser.add_argument("--out", type=Path, default=Path("docs/eval_report.md"))
    parser.add_argument("--json", type=Path, help="also write the raw metrics here")
    args = parser.parse_args(argv)

    with httpx.Client(base_url=args.api, timeout=TIMEOUT_S) as client:
        report, metrics, outcomes = evaluate(client, args.data_dir, args.api)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"{metrics['passed']}/{metrics['cases']} cases passed, "
          f"{metrics['errors']} request errors -> {args.out}")
    return 1 if metrics["errors"] == metrics["cases"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
