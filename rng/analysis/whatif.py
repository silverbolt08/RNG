"""What-if: re-run the bridge under a changed input. Owner: teammate C.

    what_if_tariff(clean, period, compare_to, model, scale) -> WhatIf

The demo's re-plan item (guide section 9.1). With a static dataset there is
no late data drop to diff against, so this is how the system shows it can
re-plan: change one input, re-run the 16 counterfactuals, and diff the new
bridge against the base one, cause by cause.

Fully deterministic -- no LLM, no new data, no ingest. "What if the tariff
had been 10% lower?" is scale=0.9: every price in force during `period` is
multiplied by it, the reference quarter's prices are untouched, and both
bridges are computed the same way. The bill's measured kWh do not change,
so the tariff term moves and the others move only by the price of the same
energy.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pandas as pd

from rng.analysis.baseline import BaselineModel
from rng.analysis.decompose import Bridge, _bridge, _setup
from rng.api.schemas import Cause
from rng.ingest.clean import CleanTables
from rng.periods import period_bounds, to_utc


@dataclass(frozen=True)
class WhatIf:
    scale: float
    base: Bridge
    scenario: Bridge
    delta: dict[Cause, float]        # currency, scenario minus base
    delta_pp: dict[Cause, float]     # pp of the (unchanged) reference bill
    description: str


def scale_tariff(tariff: pd.DataFrame, period: str, scale: float) -> pd.DataFrame:
    """Prices in force during `period` multiplied by `scale`.

    A window straddling the period's edge is split there, so the reference
    quarter keeps its real prices even when one window covers both.
    """
    start, end = (pd.Timestamp(d, tz="UTC") for d in period_bounds(period))
    t = tariff.copy()
    t["valid_from"] = to_utc(t["valid_from"]).to_numpy()
    t["valid_to"] = to_utc(t["valid_to"]).to_numpy()
    rows = []
    for _, r in t.iterrows():
        lo, hi = r["valid_from"], r["valid_to"]
        cuts = sorted({lo, hi, *(c for c in (start, end) if lo < c < hi)})
        for a, b in zip(cuts, cuts[1:], strict=False):
            piece = r.copy()
            piece["valid_from"], piece["valid_to"] = a, b
            if a >= start and b <= end:
                piece["price_per_kwh"] = float(r["price_per_kwh"]) * scale
            rows.append(piece)
    return pd.DataFrame(rows).reset_index(drop=True)


def what_if_tariff(clean: CleanTables, period: str, compare_to: str,
                   model: BaselineModel, scale: float) -> WhatIf:
    if scale <= 0:
        raise ValueError(f"tariff scale must be positive, got {scale}")
    base = _bridge(_setup(clean, period, compare_to), model, 0)
    changed = replace(clean, tariff=scale_tariff(clean.tariff, period, scale))
    scenario = _bridge(_setup(changed, period, compare_to), model, 0)
    delta = {c: scenario.effects[c] - base.effects[c] for c in Cause}
    ref = base.reference_cost
    delta_pp = {c: 100.0 * v / ref for c, v in delta.items()}
    change = f"{abs(1 - scale):.0%} {'lower' if scale < 1 else 'higher'}"
    return WhatIf(
        scale=scale, base=base, scenario=scenario, delta=delta, delta_pp=delta_pp,
        description=(
            f"With {period} prices {change}, the bill would have changed by "
            f"{scenario.total_pct:.2f}% instead of {base.total_pct:.2f}%. "
            f"The tariff term moves from {base.pct_points(Cause.TARIFF):.2f} to "
            f"{scenario.pct_points(Cause.TARIFF):.2f} pp; energy use is unchanged, "
            "so the other terms move only by the price of the same kWh."))
