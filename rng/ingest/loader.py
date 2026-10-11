"""CSV -> normalised Annex B frames. Owner: teammate B.

The CLI loader and POST /ingest are the same code path; /ingest is a thin
wrapper over this and nothing more.

Rules this must obey:
  * Extra columns are allowed and columns may arrive in any order. Only the
    Annex B columns must exist. The judges load files we have not seen.
  * Never assume meter names, the number of sub-meters, or which quarters
    are present.
  * A row that cannot be coerced is REJECTED and counted, not silently
    dropped: /ingest must return rows_loaded and rows_rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "meter_15min": ("meter_id", "ts", "kwh"),
    "tariff": ("valid_from", "valid_to", "period", "price_per_kwh"),
    "production": ("date", "product", "units", "shift_pattern", "is_holiday"),
    "weather": ("date", "avg_temp_c", "cooling_degree_days",
                "heating_degree_days"),
}


@dataclass
class RawTables:
    meter_15min: pd.DataFrame
    tariff: pd.DataFrame
    production: pd.DataFrame
    weather: pd.DataFrame


@dataclass
class LoadResult:
    table: str
    frame: pd.DataFrame
    rows_loaded: int
    rows_rejected: int
    messages: list[str]


def load_csv(path: str | Path, table: str) -> LoadResult:
    """Read one CSV, check the Annex B columns exist, coerce types."""
    raise NotImplementedError


def load_dir(data_dir: str | Path) -> RawTables:
    """Load the four tables from a directory of <table>.csv files."""
    raise NotImplementedError
