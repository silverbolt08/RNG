"""The deterministic handlers: one per kind of question.

Each is a lookup of a precomputed result plus, at most, a sort. None of them
calls an LLM. Each writes every figure it surfaces into the registry; nothing
reaches the writer by any other route.

A handler returns a dict of state updates, or {"missing": reason} when the
result it needs was never computed.
"""

from __future__ import annotations

from typing import Any

from rng.api.schemas import BridgeItem, Cause, Recommendation
from rng.config import assumptions
from rng.graph.registry import Registry
from rng.graph.store_io import Store

CAUSE_LABEL = {
    "tariff": "tariff (price)", "product_mix": "product mix",
    "volume": "production volume", "weather": "weather", "waste": "waste",
}
PAIR_KINDS = {"bridge", "bill_total", "savings", "ranking", "what_if"}
TOP_N = 5


def _quote_text_numbers(reg: Registry, text: str, key: str, src: str, ev: str) -> None:
    """Numbers inside tool-written text (a finding, a rationale) are tool
    output too. Register them, so quoting the sentence is not a violation."""
    for i, (_, value, _) in enumerate(reg.numerals(text or "")):
        reg.put(f"{key}.quoted.{i}", value, "quoted", src=src, ev=ev,
                label=f"figure quoted in {key}")


def _bridge_into(reg: Registry, b: dict, period: str, compare_to: str,
                 prefix: str = "") -> list[BridgeItem]:
    ccy = assumptions().currency
    ev = f"bridge:{period}:vs:{compare_to}"
    reg.put(f"{prefix}bill.current", b["current_cost"], ccy, src="decompose_change",
            ev=f"meter_15min:main:{period}", label=f"{prefix}Bill for {period}")
    reg.put(f"{prefix}bill.reference", b["reference_cost"], ccy,
            src="decompose_change", ev=f"meter_15min:main:{compare_to}",
            label=f"{prefix}Bill for {compare_to}")
    reg.put(f"{prefix}bill.change_pct", b["total_pct"], "%", src="decompose_change",
            ev=ev, label=f"{prefix}Change in the bill, {period} vs {compare_to}")
    items = [BridgeItem.model_validate(i) for i in b["items"]]
    for item in items:
        cause = item.cause.value
        label = CAUSE_LABEL[cause]
        cev = f"bridge:{cause}:{period}"
        reg.put(f"{prefix}bridge.{cause}.pp", item.pct_points, "pp",
                src="decompose_change", ev=cev,
                label=f"{prefix}Effect of {label}, percentage points of the bill")
        reg.put(f"{prefix}bridge.{cause}.low", item.range[0], "pp",
                src="decompose_change", ev=cev, label=f"{prefix}{label} low estimate")
        reg.put(f"{prefix}bridge.{cause}.high", item.range[1], "pp",
                src="decompose_change", ev=cev, label=f"{prefix}{label} high estimate")
        if item.amount is not None:
            reg.put(f"{prefix}bridge.{cause}.amount", item.amount, ccy,
                    src="decompose_change", ev=cev,
                    label=f"{prefix}Effect of {label} in {ccy}")
    return items


def _recommendations(store: Store, reg: Registry, period: str,
                     compare_to: str) -> list[Recommendation]:
    raw = store.cached("recommendations", period, compare_to) or []
    recs = [Recommendation.model_validate(r) for r in raw]
    ccy = assumptions().currency
    for i, rec in enumerate(recs[:TOP_N]):
        ev = rec.evidence_refs[0]
        imp = rec.estimated_impact
        for name, rng_, unit, label in (
                ("kwh", imp.saving_kwh_per_year, "kWh", "energy saved per year"),
                ("money", imp.saving_money_per_year, ccy, f"money saved per year ({ccy})"),
                ("co2e", imp.saving_co2e_t_per_year, "tCO2e", "CO2e avoided per year"),
                ("payback", imp.payback_months, "months", "payback period")):
            reg.put(f"rec.{i}.{name}.low", rng_[0], unit, src="estimate_savings",
                    ev=ev, label=f"Action {i + 1} ({rec.action}): {label}, low")
            reg.put(f"rec.{i}.{name}.high", rng_[1], unit, src="estimate_savings",
                    ev=ev, label=f"Action {i + 1} ({rec.action}): {label}, high")
        _quote_text_numbers(reg, f"{rec.action} {rec.rationale}", f"rec.{i}",
                            "estimate_savings", ev)
    return recs


def _limitations(store: Store, period: str, extra: list[str]) -> list[str]:
    out = list(extra)
    report = store.cached("cleaning_report", period) or {}
    if report.get("intervals_imputed"):
        out.append(f"{report['intervals_imputed']} missing meter intervals were imputed.")
    if report.get("resets_adjusted"):
        out.append(f"{report['resets_adjusted']} meter reset(s) were corrected.")
    if report.get("suspected_holidays"):
        out.append("Days logged as normal shifts but with near-zero production "
                   f"were treated as holidays: {', '.join(report['suspected_holidays'])}.")
    return out


# -- handlers ---------------------------------------------------------------

def bridge(store: Store, state: dict) -> dict[str, Any]:
    period, compare_to = state["period"], state["compare_to"]
    b = store.cached("bridge", period, compare_to)
    if not b:
        return {"missing": f"no decomposition has been computed for {period} "
                           f"against {compare_to}"}
    reg = Registry()
    reg.name(*state.get("assets", []))
    items = _bridge_into(reg, b, period, compare_to)
    reg.put("baseline.r2", b.get("r2", 0.0), "r2", src="build_baseline",
            ev=f"baseline:{compare_to}", label="Baseline fit R-squared")

    findings = store.cached("findings", period, compare_to) or []
    notes, top = [], None
    if findings:
        top = findings[0]
        ev = (top.get("evidence_refs") or [f"meter_15min:{top['asset']}:{period}"])[0]
        reg.put("finding.0.kwh_per_year", top["kwh_per_year"], "kWh",
                src="detect_leaks", ev=ev,
                label=f"Estimated waste on {top['asset']}, kWh per year")
        _quote_text_numbers(reg, top.get("description", ""), "finding.0",
                            "detect_leaks", ev)
        notes.append(f"After removing tariff, volume, mix and weather, the "
                     f"evidence of waste points to {top['asset']}: "
                     f"{top.get('description', '')}")
    else:
        notes.append("No asset shows a fault pattern. Do not blame any machine; "
                     "the change is explained by the four modelled causes.")
    modelled = sorted((i for i in items if i.cause != Cause.WASTE),
                      key=lambda i: abs(i.pct_points), reverse=True)
    notes.append("Causes in order of size: "
                 + ", ".join(CAUSE_LABEL[i.cause.value] for i in modelled) + ".")

    recs = _recommendations(store, reg, period, compare_to)
    r2 = b.get("r2", 0.0)
    return {
        "registry": reg, "bridge": items, "recommendations": recs[:3],
        "total_pct_points": b["total_pct"], "notes": notes,
        "facts": {"order": [i.cause.value for i in modelled],
                  "finding": top, "ccy": assumptions().currency},
        "limitations": _limitations(store, period, [
            f"Baseline (R-squared {r2:.2f}) is fitted on {compare_to} only and is "
            "linear in degree-days; a non-linear weather response lands in the "
            "waste residual.",
            "Waste is the unexplained residual, not a separate measurement."]),
        "trace": [
            ("build_baseline", {"fitted_on": compare_to}, {"r2": round(r2, 3)}),
            ("decompose_change", {"period": period, "compare_to": compare_to},
             {i.cause.value: i.pct_points for i in items}
             | {"total": round(b["total_pct"], 2)}),
            ("detect_leaks", {"requires": "bridge"},
             {"findings": len(findings),
              **({"asset": top["asset"]} if top else {})}),
        ],
    }


def bill_total(store: Store, state: dict) -> dict[str, Any]:
    period, compare_to = state["period"], state["compare_to"]
    b = store.cached("bridge", period, compare_to)
    if not b:
        return {"missing": f"no bill has been computed for {period}"}
    reg = Registry()
    _bridge_into(reg, b, period, compare_to)
    return {"registry": reg, "total_pct_points": None,
            "facts": {"ccy": assumptions().currency}, "notes": [],
            "limitations": _limitations(store, period, []),
            "trace": [("price_meter_readings", {"period": period},
                       {"bill": round(b["current_cost"], 2)})]}


def top_users(store: Store, state: dict) -> dict[str, Any]:
    period = state["period"]
    rows = store.cached("asset_totals", period)
    if not rows:
        return {"missing": f"no asset totals have been computed for {period}"}
    rows = sorted(rows, key=lambda r: r.get("rank", 0) or 0)
    asked = (state.get("intent") or {}).get("asset")
    # "Which use the least?" is the same lookup read from the other end.
    least = (state.get("intent") or {}).get("order") == "asc"
    shown = rows[::-1][:TOP_N] if least else rows[:TOP_N]
    if asked and asked not in [r["asset"] for r in shown]:
        shown += [r for r in rows if r["asset"] == asked]
    reg = Registry()
    reg.name(*[r["asset"] for r in rows])
    ccy = assumptions().currency
    for r in shown:
        ev = f"meter_15min:{r['asset']}:{period}"
        reg.put(f"asset.{r['asset']}.kwh", r["kwh"], "kWh", src="rank_assets",
                ev=ev, label=f"Energy used by {r['asset']} in {period}")
        reg.put(f"asset.{r['asset']}.share", r["share_pct"], "share_pct",
                src="rank_assets", ev=ev,
                label=f"{r['asset']} share of the main meter, percent")
        if r.get("cost") is not None:
            reg.put(f"asset.{r['asset']}.cost", r["cost"], ccy, src="rank_assets",
                    ev=ev, label=f"Cost of {r['asset']} in {period} ({ccy})")
    return {"registry": reg, "notes": ["Measured sub-meter data, not a model.",
                      "The assets are listed "
                      + ("smallest first: the question asks for the LEAST."
                         if least else "largest first.")],
            "facts": {"order": [r["asset"] for r in shown[:3]], "asked": asked,
                      "direction": "smallest" if least else "largest",
                      "ccy": ccy},
            "limitations": _limitations(store, period, [
                "Sub-meters do not sum exactly to the main meter; shares are "
                "of the main meter reading."]),
            "trace": [("rank_assets", {"period": period},
                       {r["asset"]: round(r["share_pct"], 1) for r in shown})]}


def idle_load(store: Store, state: dict) -> dict[str, Any]:
    period = state["period"]
    stats = store.cached("idle_load", period)
    if not stats:
        return {"missing": f"no idle-load analysis has been computed for {period}"}
    least = (state.get("intent") or {}).get("order") == "asc"
    rows = sorted(stats.values(), key=lambda r: r["idle_kwh"], reverse=not least)
    asked = (state.get("intent") or {}).get("asset")
    if asked:
        rows = [r for r in rows if r["asset"] == asked] or rows
    reg = Registry()
    reg.name(*[r["asset"] for r in stats.values()])
    ccy = assumptions().currency
    subs = [r for r in stats.values() if r["asset"].lower() != "main"]
    reg.put("idle.total_kwh", sum(r["idle_kwh"] for r in subs), "kWh",
            src="find_idle_load", ev=f"meter_15min:idle:{period}",
            label=f"Energy used by all sub-meters while nothing was produced, {period}")
    shown = [r for r in rows if r["asset"].lower() != "main"][:TOP_N] or rows[:TOP_N]
    for r in shown:
        ev = f"meter_15min:{r['asset']}:{period}:idle"
        a = r["asset"]
        reg.put(f"idle.{a}.kwh", r["idle_kwh"], "kWh", src="find_idle_load", ev=ev,
                label=f"Idle energy of {a}")
        reg.put(f"idle.{a}.hours", r["idle_hours"], "hours", src="find_idle_load",
                ev=ev, label=f"Hours with no production counted for {a}")
        reg.put(f"idle.{a}.kw", r["median_idle_kw"], "kW", src="find_idle_load",
                ev=ev, label=f"Typical idle load of {a}, kW")
        reg.put(f"idle.{a}.night_share", r["night_share"], "ratio",
                src="find_idle_load", ev=ev,
                label=f"Night load of {a} as a fraction of its day load")
        if r.get("cost") is not None:
            reg.put(f"idle.{a}.cost", r["cost"], ccy, src="find_idle_load", ev=ev,
                    label=f"Cost of idle energy of {a} ({ccy})")
    return {"registry": reg, "notes": [
                "Idle means intervals with no production: nights, weekends and holidays."],
            "facts": {"order": [r["asset"] for r in shown[:3]], "ccy": ccy,
                      "direction": "smallest" if least else "largest"},
            "limitations": _limitations(store, period, [
                "Idle load is not all waste: some standby load is unavoidable."]),
            "trace": [("find_idle_load", {"period": period},
                       {r["asset"]: {"idle_kwh": round(r["idle_kwh"]),
                                     "night_share": round(r["night_share"], 2)}
                        for r in shown})]}


def savings(store: Store, state: dict) -> dict[str, Any]:
    period, compare_to = state["period"], state["compare_to"]
    if store.cached("bridge", period, compare_to) is None:
        return {"missing": "no decomposition exists yet, and actions are only "
                           "recommended after tariff, volume, mix and weather "
                           "have been removed"}
    # "Not computed" is not "nothing to do". An empty list means the analysis
    # ran and found no waste; a missing one means it never ran.
    if store.cached("recommendations", period, compare_to) is None:
        return {"missing": "savings have not been estimated for this period, "
                           "so no action can be costed or ranked"}
    reg = Registry()
    reg.name(*state.get("assets", []))
    recs = _recommendations(store, reg, period, compare_to)
    ranking = state["kind"] == "ranking"
    tool = "rank_actions" if ranking else "estimate_savings"
    reg.put("rec.count", len(recs), "count", src=tool,
            ev=f"recommendations:{period}", label="Number of recommended actions")
    notes = ["Actions are ordered by payback period, then by ease.",
             "Every action is a suggestion that a manager must approve."]
    if not recs:
        notes = ["No controllable waste was found after removing tariff, volume, "
                 "mix and weather, so no action is recommended."]
    return {"registry": reg, "recommendations": recs, "notes": notes,
            "facts": {"ranking": ranking, "ccy": assumptions().currency,
                      "actions": [r.action for r in recs[:3]],
                      "ease": [r.estimated_impact.ease.value for r in recs[:3]]},
            "limitations": _limitations(store, period, [
                "Fix costs and the recovery fraction are assumptions, not data; "
                "payback ranges inherit their uncertainty."]),
            "trace": [(tool, {"period": period, "compare_to": compare_to},
                       {"actions": len(recs),
                        **({"first": recs[0].action} if recs else {})})]}


def what_if(store: Store, state: dict) -> dict[str, Any]:
    period, compare_to = state["period"], state["compare_to"]
    intent = state.get("intent") or {}
    base = store.cached("bridge", period, compare_to)
    if not base:
        return {"missing": f"no decomposition has been computed for {period} "
                           f"against {compare_to}, so there is nothing to vary"}
    reg = Registry()
    ccy = assumptions().currency
    _bridge_into(reg, base, period, compare_to, prefix="base.")

    if intent.get("scenario") == "fix_waste":
        waste = next(i for i in base["items"] if i["cause"] == "waste")
        amount = waste.get("amount")
        if amount is None:
            amount = base["reference_cost"] * waste["pct_points"] / 100.0
        ev = f"bridge:waste:{period}"
        reg.put("scenario.bill.current", base["current_cost"] - amount, ccy,
                src="what_if_fix_waste", ev=ev,
                label=f"Bill for {period} if the identified waste were removed")
        reg.put("scenario.bill.change_pct", base["total_pct"] - waste["pct_points"],
                "%", src="what_if_fix_waste", ev=ev,
                label="Change in the bill if the waste were removed")
        reg.put("scenario.saving", amount, ccy, src="what_if_fix_waste", ev=ev,
                label=f"Bill reduction in {period} ({ccy})")
        return {"registry": reg, "notes": [
                    "Only the waste term changes; the other four causes are untouched."],
                "facts": {"scenario": "fix_waste", "ccy": ccy},
                "bridge": [BridgeItem.model_validate(i) for i in base["items"]
                           if i["cause"] != "waste"],
                "total_pct_points": base["total_pct"] - waste["pct_points"],
                "limitations": ["Assumes the whole unexplained residual is "
                                "recoverable; in practice only part of it is."],
                "trace": [("what_if_fix_waste", {"period": period},
                           {"saving": round(amount, 2)})]}

    scale = intent.get("tariff_scale")
    if scale is None:
        return {"missing": "the what-if does not say by what percentage the "
                           "tariff should change"}
    try:
        from rng.analysis.whatif import what_if_tariff
        from rng.ingest.pipeline import current

        live = current()
        scenario = what_if_tariff(live.clean, period, compare_to, live.model,
                                  scale).scenario
    except Exception as exc:   # engine not available: say so, never guess
        return {"missing": f"the what-if engine is not available ({type(exc).__name__})"}
    s = {"current_cost": scenario.current_cost,
         "reference_cost": scenario.reference_cost, "total_pct": scenario.total_pct,
         "items": [i.model_dump(mode="json") for i in scenario.to_items()]}
    items = _bridge_into(reg, s, period, compare_to, prefix="scenario.")
    reg.put("scenario.tariff_change_pct", (scale - 1.0) * 100.0, "%",
            src="what_if_tariff", ev=f"tariff:{period}",
            label="Assumed change in the tariff, percent")
    reg.put("scenario.bill.delta", scenario.current_cost - base["current_cost"], ccy,
            src="what_if_tariff", ev=f"tariff:{period}",
            label=f"Difference in the {period} bill versus what actually happened")
    return {"registry": reg, "bridge": items, "total_pct_points": scenario.total_pct,
            "notes": ["Entries starting 'base.' are what actually happened; "
                      "'scenario.' is the what-if.",
                      "Only the price changes. Energy use is unchanged."],
            "facts": {"scenario": "tariff", "ccy": ccy},
            "limitations": ["The what-if rescales current-period prices only; it "
                            "assumes consumption would not have responded to price."],
            "trace": [("what_if_tariff", {"period": period, "scale": scale},
                       {"bill": round(scenario.current_cost, 2),
                        "total": round(scenario.total_pct, 2)})]}


HANDLERS = {"bridge": bridge, "bill_total": bill_total, "top_users": top_users,
            "idle_load": idle_load, "savings": savings, "ranking": savings,
            "what_if": what_if}
