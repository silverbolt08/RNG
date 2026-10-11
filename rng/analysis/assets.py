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

from rng.ingest.clean import CleanTables


@dataclass(frozen=True)
class AssetTotal:
    asset: str
    kwh: float
    share_pct: float      # of the main meter
    cost: float
    rank: int


def asset_totals(clean: CleanTables, period: str) -> list[AssetTotal]:
    """Every sub-meter's energy, share of main and cost, largest first."""
    raise NotImplementedError
