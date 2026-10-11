"""R8 savings estimate and R9 ranking. Owner: teammate A.

    estimate_savings(findings, price_per_kwh=None) -> list[Recommendation]

Every figure is a low and a high value (Annex A). kWh, money and CO2e ranges
come from the finding's energy and the assumption ranges in
rng.config.assumptions() -- recovery_fraction, fix_cost, the emission factor.
Fix costs are not in any input file: they are our assumption and must be
declared, which is why they live in config and not here.

No range is invented, and no number is written by the LLM. Every
recommendation carries at least one evidence_ref and requires_approval=True:
actions are suggestions, a manager approves them.

Ranking (R9): payback period first, then ease.

A Finding carries energy, not money, so the price has to come from the
tariff. Pass `price_per_kwh` (average_price(clean.tariff)) when the tables are
in hand; without it the tariff file in the configured data directory is read.
The size of the waste is only ever what the Finding says it is -- this module
sits behind the import wall and never sees the generator.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from rng.api.schemas import Ease, EstimatedImpact, Finding, Recommendation
from rng.config import Assumptions, assumptions, settings
from rng.periods import to_utc

_INTERVAL = pd.Timedelta(minutes=15)
# Any Monday-to-Sunday week: the latest prices are laid over it to weight peak
# against off-peak hours. Only the weekday pattern matters, not the date.
_ANY_WEEK = pd.date_range("2024-01-01", periods=7 * 96, freq=_INTERVAL, tz="UTC")
_EASE_ORDER = {Ease.EASY: 0, Ease.MODERATE: 1, Ease.HARD: 2}


@dataclass(frozen=True)
class _Fix:
    cost_key: str       # key into Assumptions.fix_cost
    action: str         # formatted with the asset name
    ease: Ease


_LEAK = _Fix("compressor_leak_repair",
             "Find and repair the continuous loss on {asset}", Ease.EASY)
_SHUTDOWN = _Fix("idle_shutdown_schedule",
                 "Schedule {asset} to shut down outside production hours",
                 Ease.EASY)
# Idle load on these kinds of plant has a more specific remedy than a blanket
# shutdown. Matched on words in the asset name; anything else gets _SHUTDOWN.
_IDLE_BY_WORD: tuple[tuple[tuple[str, ...], _Fix], ...] = (
    (("light", "lamp"),
     _Fix("lighting_controls",
          "Fit occupancy and daylight controls on {asset}", Ease.HARD)),
    (("hvac", "chill", "cool", "heat", "vent"),
     _Fix("hvac_setpoint_change",
          "Set back the out-of-hours setpoints on {asset}", Ease.EASY)),
    (("compress", "air"),
     _Fix("compressor_sequencing",
          "Sequence {asset} to unload when nothing is being produced",
          Ease.MODERATE)),
)


def _fix_for(finding: Finding) -> _Fix:
    if finding.kind in ("leak", "step_change"):
        return _LEAK
    name = finding.asset.lower()
    for words, fix in _IDLE_BY_WORD:
        if any(w in name for w in words):
            return fix
    return _SHUTDOWN


def average_price(tariff: pd.DataFrame) -> float:
    """Time-weighted price per kWh over one week at the latest tariff.

    Savings are a year ahead, so they are priced at the most recent window,
    with peak and off-peak hours weighted as resolve_prices() applies them.
    """
    from rng.ingest.tariff import resolve_prices

    if tariff.empty:
        raise ValueError("no tariff rows to price savings with")
    starts = to_utc(tariff["valid_from"])
    latest = tariff[(starts == starts.max()).to_numpy()].copy()
    latest["valid_from"] = _ANY_WEEK[0]
    latest["valid_to"] = _ANY_WEEK[-1] + _INTERVAL
    prices, _ = resolve_prices(latest, pd.Series(_ANY_WEEK))
    return float(prices.mean())


def _configured_price() -> float:
    from rng.ingest.loader import load_csv

    path = settings().data_dir / "tariff.csv"
    if not path.is_file():
        raise ValueError(
            f"no price_per_kwh given and no tariff at {path}; a saving is "
            "never priced at a guessed rate")
    return average_price(load_csv(path, "tariff").frame)


def _recommend(finding: Finding, price: float, a: Assumptions) -> Recommendation:
    fix = _fix_for(finding)
    if fix.cost_key not in a.fix_cost:
        raise ValueError(f"no fix cost declared for {fix.cost_key!r}")
    cost_low, cost_high = a.fix_cost[fix.cost_key]
    share_low, share_high = a.recovery_fraction

    kwh = (finding.kwh_per_year * share_low, finding.kwh_per_year * share_high)
    money = (kwh[0] * price, kwh[1] * price)
    factor = a.emission_factor_kg_co2e_per_kwh / 1000.0     # kg -> tonnes
    # Cheapest fix against the largest saving, and the reverse.
    payback = (12.0 * cost_low / money[1], 12.0 * cost_high / money[0])

    rationale = (
        f"{finding.description.rstrip('. ')}. Assumes {share_low:.0%}-"
        f"{share_high:.0%} of {finding.kwh_per_year:,.0f} kWh a year is "
        f"recoverable, priced at {a.currency} {price:.2f} per kWh (latest "
        f"tariff, time-weighted). Fix cost {a.currency} {cost_low:,.0f}-"
        f"{cost_high:,.0f} is our estimate and is not in the data.")
    refs = list(dict.fromkeys(
        [*finding.evidence_refs, f"finding:{finding.finding_id}"]))
    return Recommendation(
        action=fix.action.format(asset=finding.asset),
        rationale=rationale,
        evidence_refs=refs,
        confidence=finding.confidence,
        estimated_impact=EstimatedImpact(
            saving_kwh_per_year=(round(kwh[0]), round(kwh[1])),
            saving_money_per_year=(round(money[0]), round(money[1])),
            saving_co2e_t_per_year=(round(kwh[0] * factor, 2),
                                    round(kwh[1] * factor, 2)),
            payback_months=(round(payback[0], 1), round(payback[1], 1)),
            ease=fix.ease,
        ),
        requires_approval=True,
    )


def _rank_key(rec: Recommendation) -> tuple[float, int, float]:
    impact = rec.estimated_impact
    return (sum(impact.payback_months) / 2.0, _EASE_ORDER[impact.ease],
            -impact.saving_money_per_year[1])


def estimate_savings(findings: list[Finding],
                     price_per_kwh: float | None = None) -> list[Recommendation]:
    """Costed recommendations, ranked by payback then ease."""
    worth_fixing = [f for f in findings if f.kwh_per_year > 0]
    if not worth_fixing:
        return []
    price = _configured_price() if price_per_kwh is None else float(price_per_kwh)
    if price <= 0:
        raise ValueError(f"price per kWh must be positive, got {price}")
    a = assumptions()
    return sorted((_recommend(f, price, a) for f in worth_fixing), key=_rank_key)
