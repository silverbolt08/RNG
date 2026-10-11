"""The slot-filled template writer.

Used when MOCK_LLM is on, when the provider is unreachable, and as the last
resort when the LLM's brief fails the number check twice. Every figure is
pulled from the registry by key -- there is no arithmetic and no literal
number in this file, so its output passes the check by construction.

The prose is plain on purpose. Worst case is a stilted brief, never a wrong
number.
"""

from __future__ import annotations

from rng.graph.handlers import CAUSE_LABEL
from rng.graph.registry import Registry

SAFE = ("The analysis completed. The figures are in the structured fields of "
        "this response; the written summary was withheld because it did not "
        "pass the number check.")


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _bridge(reg: Registry, st: dict) -> str:
    f, period, ref = st["facts"], st["period"], st["compare_to"]
    ccy = f["ccy"]
    up = reg.value("bill.change_pct") >= 0
    out = [f"The electricity bill for {period} was {reg.show('bill.current')} {ccy}, "
           f"{reg.show('bill.change_pct', True)}% {'higher' if up else 'lower'} than "
           f"in {ref} ({reg.show('bill.reference')} {ccy})."]
    parts = []
    for cause in f["order"]:
        added = reg.value(f"bridge.{cause}.pp") >= 0
        parts.append(f"{CAUSE_LABEL[cause]} {'added' if added else 'took off'} "
                     f"{reg.show(f'bridge.{cause}.pp', True)} points")
    out.append("By cause: " + _join(parts) + ".")
    top = f["order"][0]
    out.append(f"The largest effect, {CAUSE_LABEL[top]}, is estimated between "
               f"{reg.show(f'bridge.{top}.low')} and "
               f"{reg.show(f'bridge.{top}.high')} points.")
    waste = reg.show("bridge.waste.pp", True)
    finding = f.get("finding")
    if finding:
        out.append(f"Only after removing those effects is anything called waste: "
                   f"{waste} points remain, and the evidence points to "
                   f"{finding['asset']}. {finding.get('description', '').strip()}")
    else:
        out.append(f"After removing those effects {waste} points remain "
                   "unexplained and no asset shows a fault pattern, so no "
                   "machine is blamed.")
    return " ".join(out)


def _bill_total(reg: Registry, st: dict) -> str:
    ccy = st["facts"]["ccy"]
    return (f"The electricity bill for {st['period']} was "
            f"{reg.show('bill.current')} {ccy}. For {st['compare_to']} it was "
            f"{reg.show('bill.reference')} {ccy}, a change of "
            f"{reg.show('bill.change_pct')}%.")


def _asset_line(reg: Registry, asset: str) -> str:
    return (f"{asset} ({reg.show(f'asset.{asset}.kwh')} kWh, "
            f"{reg.show(f'asset.{asset}.share')}% of the main meter)")


def _top_users(reg: Registry, st: dict) -> str:
    f = st["facts"]
    order = f["order"]
    out = [f"In {st['period']} the largest energy user was "
           f"{_asset_line(reg, order[0])}."]
    if len(order) > 1:
        out.append("It is followed by "
                   + _join([_asset_line(reg, a) for a in order[1:]]) + ".")
    asked = f.get("asked")
    if asked and asked not in order and f"asset.{asked}.kwh" in reg:
        out.append(f"You asked about {_asset_line(reg, asked)}.")
    out.append("These are measured sub-meter readings, not model estimates.")
    return " ".join(out)


def _idle_line(reg: Registry, asset: str) -> str:
    return (f"{asset} ({reg.show(f'idle.{asset}.kwh')} kWh over "
            f"{reg.show(f'idle.{asset}.hours')} hours, about "
            f"{reg.show(f'idle.{asset}.kw')} kW)")


def _idle_load(reg: Registry, st: dict) -> str:
    order = st["facts"]["order"]
    return (f"While nothing was being produced in {st['period']}, the sub-meters "
            f"still used {reg.show('idle.total_kwh')} kWh. The largest idle loads "
            f"are {_join([_idle_line(reg, a) for a in order])}. This is energy "
            "used at night, at weekends and on holidays; part of it is "
            "unavoidable standby.")


def _action_line(reg: Registry, i: int, ccy: str) -> str:
    return (f"It would save {reg.show(f'rec.{i}.kwh.low')} to "
            f"{reg.show(f'rec.{i}.kwh.high')} kWh a year, worth "
            f"{reg.show(f'rec.{i}.money.low')} to "
            f"{reg.show(f'rec.{i}.money.high')} {ccy} and "
            f"{reg.show(f'rec.{i}.co2e.low')} to "
            f"{reg.show(f'rec.{i}.co2e.high')} tonnes of CO2e, and pays back in "
            f"{reg.show(f'rec.{i}.payback.low')} to "
            f"{reg.show(f'rec.{i}.payback.high')} months")


def _savings(reg: Registry, st: dict) -> str:
    f = st["facts"]
    actions, ccy = f["actions"], f["ccy"]
    if not actions:
        return (f"No action is recommended for {st['period']}. After removing "
                "the effects of tariff, volume, product mix and weather, no "
                "controllable waste was found, so no machine is blamed.")
    lead = ("The action that pays back fastest is" if f.get("ranking")
            else "The first thing to do is")
    out = [f"{lead}: {actions[0]}. {_action_line(reg, 0, ccy)} "
           f"(ease: {f['ease'][0]})."]
    for i, action in enumerate(actions[1:], start=1):
        out.append(f"Next: {action}, with a payback of "
                   f"{reg.show(f'rec.{i}.payback.low')} to "
                   f"{reg.show(f'rec.{i}.payback.high')} months.")
    out.append("Actions are ranked by payback period, then by ease, and each "
               "one needs a manager's approval.")
    return " ".join(out)


def _what_if(reg: Registry, st: dict) -> str:
    f, period, ref = st["facts"], st["period"], st["compare_to"]
    ccy = f["ccy"]
    if f["scenario"] == "fix_waste":
        return (f"If the identified waste were removed, the bill for {period} "
                f"would have been {reg.show('scenario.bill.current')} {ccy} "
                f"instead of {reg.show('base.bill.current')} {ccy}, "
                f"{reg.show('scenario.saving', True)} {ccy} less. Against {ref} "
                f"the bill would have changed by "
                f"{reg.show('scenario.bill.change_pct')}% instead of "
                f"{reg.show('base.bill.change_pct')}%. The other causes are "
                "unchanged.")
    lower = reg.value("scenario.tariff_change_pct") < 0
    return (f"If the {period} tariff had been "
            f"{reg.show('scenario.tariff_change_pct', True)}% "
            f"{'lower' if lower else 'higher'}, the bill would have been "
            f"{reg.show('scenario.bill.current')} {ccy} instead of "
            f"{reg.show('base.bill.current')} {ccy}, a difference of "
            f"{reg.show('scenario.bill.delta', True)} {ccy}. Against {ref} the "
            f"bill would have changed by {reg.show('scenario.bill.change_pct')}% "
            f"instead of {reg.show('base.bill.change_pct')}%. The tariff effect "
            f"becomes {reg.show('scenario.bridge.tariff.pp')} points instead of "
            f"{reg.show('base.bridge.tariff.pp')}; energy use is unchanged.")


_TEMPLATES = {"bridge": _bridge, "bill_total": _bill_total, "top_users": _top_users,
              "idle_load": _idle_load, "savings": _savings, "ranking": _savings,
              "what_if": _what_if}


def render(kind: str, reg: Registry, state: dict) -> str:
    return _TEMPLATES[kind](reg, state)


# -- answers that carry no numbers at all -----------------------------------

def stop_message(answer_type: str, reason: str, periods: list[str],
                 assets: list[str]) -> str:
    covers = (f"The loaded data covers {_join(periods)}" if periods
              else "No data is loaded")
    if answer_type == "refused":
        return ("I can't answer that. Every figure I report has to come from a "
                f"calculation on the loaded data, and {reason}.")
    if answer_type == "clarification_needed":
        meters = (f" Meters in the data: {_join(assets)}." if assets else "")
        return f"I need one more detail: {reason}. {covers}.{meters}"
    return (f"That is not in the data: {reason}. {covers}: meter readings, "
            "tariff, production and weather. I will not estimate what the data "
            "does not contain.")
