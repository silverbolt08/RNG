"""R5 asset breakdown -- the cheap tier. Owner: teammate B (after the store).

A groupby and a sort over MEASURED data. Deliberately not routed through the
fitted baseline: "which assets use the most energy?" asks for measurements,
and a model would answer with predictions.

Independent of the bridge, so these answers still work if the decomposition
is broken. Cache key: (dataset_id, period) -- no compare_to.

Never assume meter names or how many sub-meters there are. The main meter is
whichever meter_id the loader identified as the main supply.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from rng.ingest.clean import CleanTables
from rng.ingest.loader import main_meter
from rng.ingest.tariff import resolve_prices
from rng.periods import in_period


@dataclass(frozen=True)
class AssetTotal:
    asset: str
    kwh: float
    share_pct: float      # of the main meter
    cost: float
    rank: int


def asset_totals(clean: CleanTables, period: str) -> list[AssetTotal]:
    """Every sub-meter's energy, share of main and cost, largest first."""
    meter = clean.meter[in_period(clean.meter["ts"], period).to_numpy()]
    if meter.empty:
        return []
    main = clean.main_meter or main_meter(clean.meter)

    stamps = pd.Series(meter["ts"].unique())
    prices, _ = resolve_prices(clean.tariff, stamps)
    price = meter["ts"].map(pd.Series(prices, index=stamps))
    totals = (meter.assign(cost=meter["kwh"] * price)
              .groupby("meter_id")[["kwh", "cost"]].sum())
    main_kwh = float(totals.loc[main, "kwh"]) if main in totals.index else 0.0
    subs = totals.drop(index=main, errors="ignore").sort_values("kwh", ascending=False)
    return [
        AssetTotal(asset=str(asset), kwh=float(row.kwh),
                   share_pct=100.0 * float(row.kwh) / main_kwh if main_kwh else 0.0,
                   cost=float(row.cost), rank=rank)
        for rank, (asset, row) in enumerate(subs.iterrows(), start=1)
    ]
