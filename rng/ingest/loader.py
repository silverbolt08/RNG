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

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "meter_15min": ("meter_id", "ts", "kwh"),
    "tariff": ("valid_from", "valid_to", "period", "price_per_kwh"),
    "production": ("date", "product", "units", "shift_pattern", "is_holiday"),
    "weather": ("date", "avg_temp_c", "cooling_degree_days",
                "heating_degree_days"),
}

# Kept when present; never required.
OPTIONAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "tariff": ("peak_start_hour", "peak_end_hour"),
}

# Columns that may be blank without rejecting the row.
_NULLABLE = {"shift_pattern", "is_holiday", "peak_start_hour", "peak_end_hour"}

_TRUE = {"true", "1", "yes", "y", "t"}
_FALSE = {"false", "0", "no", "n", "f", ""}


@dataclass
class RawTables:
    meter_15min: pd.DataFrame
    tariff: pd.DataFrame
    production: pd.DataFrame
    weather: pd.DataFrame
    loads: dict[str, LoadResult] = field(default_factory=dict)

    @property
    def rows_loaded(self) -> int:
        return sum(r.rows_loaded for r in self.loads.values())

    @property
    def rows_rejected(self) -> int:
        return sum(r.rows_rejected for r in self.loads.values())


@dataclass
class LoadResult:
    table: str
    frame: pd.DataFrame
    rows_loaded: int
    rows_rejected: int
    messages: list[str]


def _timestamps(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, utc=True, format="ISO8601", errors="coerce")


def _dates(values: pd.Series) -> pd.Series:
    """Calendar dates as tz-naive midnight timestamps."""
    return _timestamps(values).dt.tz_localize(None).dt.normalize()


def _bools(values: pd.Series) -> pd.Series:
    text = values.str.strip().str.lower()
    out = pd.Series(pd.NA, index=values.index, dtype="boolean")
    out[text.isin(_TRUE)] = True
    out[text.isin(_FALSE)] = False
    return out


def _coerce(df: pd.DataFrame, table: str) -> pd.DataFrame:
    """Typed copy of the frame; uncoercible values become NA."""
    out = pd.DataFrame(index=df.index)
    for col in df.columns:
        raw = df[col]
        if col in ("ts", "valid_from", "valid_to"):
            out[col] = _timestamps(raw)
        elif col == "date":
            out[col] = _dates(raw)
        elif col in ("meter_id", "product", "period", "shift_pattern"):
            text = raw.str.strip()
            out[col] = text.where(text != "")
        elif col == "is_holiday":
            out[col] = _bools(raw)
        elif col in ("units", "peak_start_hour", "peak_end_hour"):
            num = pd.to_numeric(raw, errors="coerce")
            # A fractional unit count is a bad value, not something to round.
            out[col] = num.where(num.isna() | (num == num.round())).astype("Int64")
        else:
            out[col] = pd.to_numeric(raw, errors="coerce").astype(float)
    if table == "tariff":
        out["period"] = (out["period"].str.lower()
                         .str.replace("-", "_").str.replace(" ", "_"))
    return out


def load_csv(path: str | Path, table: str) -> LoadResult:
    """Read one CSV, check the Annex B columns exist, coerce types."""
    if table not in REQUIRED_COLUMNS:
        raise ValueError(f"unknown table {table!r}; expected one of "
                         f"{sorted(REQUIRED_COLUMNS)}")
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [str(c).strip().lower() for c in df.columns]

    required = REQUIRED_COLUMNS[table]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{Path(path).name}: missing required column(s) "
                         f"{missing} for table {table!r}")
    keep = list(required) + [c for c in OPTIONAL_COLUMNS.get(table, ())
                             if c in df.columns]
    messages = []
    extra = [c for c in df.columns if c not in keep]
    if extra:
        messages.append(f"{table}: ignored extra column(s) {extra}")

    typed = _coerce(df[keep], table)
    strict = [c for c in keep if c not in _NULLABLE]
    # A nullable column is still bad if it held text we could not read.
    bad_text = pd.Series(False, index=df.index)
    for col in (c for c in keep if c in _NULLABLE):
        bad_text |= typed[col].isna() & (df[col].str.strip() != "")
    bad = typed[strict].isna().any(axis=1) | bad_text
    rejected = int(bad.sum())
    if rejected:
        first = df.index[bad][:5].map(lambda i: i + 2).tolist()   # 1-based + header
        messages.append(f"{table}: rejected {rejected} row(s) that could not be "
                        f"read (first file lines {first})")
    frame = typed[~bad].reset_index(drop=True)
    return LoadResult(table=table, frame=frame, rows_loaded=len(frame),
                      rows_rejected=rejected, messages=messages)


def load_dir(data_dir: str | Path) -> RawTables:
    """Load the four tables from a directory of <table>.csv files."""
    data_dir = Path(data_dir)
    missing = [t for t in REQUIRED_COLUMNS if not (data_dir / f"{t}.csv").is_file()]
    if missing:
        raise FileNotFoundError(f"{data_dir}: missing {[f'{t}.csv' for t in missing]}")
    loads = {t: load_csv(data_dir / f"{t}.csv", t) for t in REQUIRED_COLUMNS}
    return RawTables(**{t: r.frame for t, r in loads.items()}, loads=loads)


def main_meter(meter: pd.DataFrame) -> str:
    """The main supply: the meter with the largest measured total.

    Every sub-meter is a share of the main supply, so it is the largest by
    construction. Negative values (resets) are left out of the sum.
    """
    if meter.empty:
        raise ValueError("no meter data")
    totals = meter["kwh"].clip(lower=0).groupby(meter["meter_id"]).sum()
    return str(totals.idxmax())
