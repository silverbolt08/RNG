"""R1 data cleaning. Owner: teammate B.

    clean(raw) -> (CleanTables, CleaningReport)

What the data contains on purpose (guide section 4.4), and what to do:
  * Missing intervals  -> reindex each meter to a regular 15-minute grid,
    impute, and set imputed=True. Report every gap; do not hide them.
  * Meter reset        -> a cumulative counter rollover shows as a negative
    kwh. Replace it, set reset_adj=True.
  * Duplicates         -> drop, count.
  * Sub-meters do not sum to main -> report the gap against the tolerance in
    rng.config.assumptions(). Do NOT rescale meters to force agreement.
  * Holiday logged as a normal shift -> flag days whose production is
    near zero while shift_pattern claims a working day.

The flags are preserved in meter_clean so the demo can show what cleaning did.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from rng.ingest.loader import RawTables


@dataclass
class CleanTables:
    meter: pd.DataFrame        # meter_id, ts (UTC), kwh, imputed, reset_adj
    tariff: pd.DataFrame
    production: pd.DataFrame   # + suspected_holiday (bool)
    weather: pd.DataFrame


@dataclass
class CleaningReport:
    rows_in: int = 0
    rows_out: int = 0
    intervals_imputed: int = 0
    resets_adjusted: int = 0
    duplicates_dropped: int = 0
    reconciliation_gap_pct: float = 0.0
    reconciliation_ok: bool = True
    suspected_holidays: list[str] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)   # meter_id, from, to, rows
    notes: list[str] = field(default_factory=list)


def clean(raw: RawTables) -> tuple[CleanTables, CleaningReport]:
    raise NotImplementedError
