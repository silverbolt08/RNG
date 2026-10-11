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

import numpy as np
import pandas as pd

from rng.config import assumptions
from rng.ingest.loader import RawTables, main_meter

INTERVAL = pd.Timedelta(minutes=15)
_WEEK = 7 * 24 * 4                     # intervals in a week
_DAY = 24 * 4
# A working day producing under this share of a normal working day is flagged.
HOLIDAY_SHARE = 0.20
_NON_WORKING = {"down", "off", "closed", "none", "holiday", "shutdown"}


@dataclass
class CleanTables:
    meter: pd.DataFrame        # meter_id, ts (UTC), kwh, imputed, reset_adj
    tariff: pd.DataFrame
    production: pd.DataFrame   # + suspected_holiday (bool)
    weather: pd.DataFrame
    main_meter: str = ""       # meter_id of the main supply, found by size


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


def _fill(kwh: pd.Series, gap: np.ndarray, reset: np.ndarray) -> pd.Series:
    """Fill one meter's missing intervals on its regular grid.

    A single reset interval sits between good readings: interpolate it. A gap
    can last a day, so a straight line would erase the load shape; use the
    same slot a week either side (a day either side if not), and only fall
    back to interpolation when neither exists.
    """
    out = kwh.copy()
    out[reset] = np.nan
    seen = out.copy()
    out[reset] = seen.interpolate(limit_area="inside")[reset]
    for lag in (_WEEK, _DAY):
        missing = out.isna().to_numpy() & gap
        if not missing.any():
            break
        guess = pd.concat([seen.shift(lag), seen.shift(-lag)], axis=1).mean(axis=1)
        out[missing] = guess[missing]
    return out.interpolate(limit_direction="both")


def _gap_runs(meter_id: str, ts: pd.Series, gap: np.ndarray) -> list[dict]:
    runs, idx = [], np.flatnonzero(gap)
    if idx.size == 0:
        return runs
    breaks = np.flatnonzero(np.diff(idx) > 1)
    for lo, hi in zip(np.r_[0, breaks + 1], np.r_[breaks, idx.size - 1], strict=True):
        runs.append({"meter_id": meter_id,
                     "from": ts.iloc[idx[lo]].isoformat(),
                     "to": ts.iloc[idx[hi]].isoformat(),
                     "rows": int(idx[hi] - idx[lo] + 1)})
    return runs


def _clean_meter(raw: pd.DataFrame, report: CleaningReport) -> pd.DataFrame:
    m = raw[["meter_id", "ts", "kwh"]].copy()
    m["ts"] = m["ts"].dt.floor(INTERVAL)
    before = len(m)
    m = m.drop_duplicates(["meter_id", "ts"], keep="first")
    report.duplicates_dropped = before - len(m)

    grid = pd.date_range(m["ts"].min(), m["ts"].max(), freq=INTERVAL)
    frames = []
    for meter_id, g in m.groupby("meter_id", sort=True):
        s = g.set_index("ts")["kwh"].reindex(grid)
        gap = s.isna().to_numpy()
        reset = (s < 0).to_numpy()
        filled = _fill(s, gap, reset)
        ts = pd.Series(grid)
        report.gaps.extend(_gap_runs(str(meter_id), ts, gap))
        frames.append(pd.DataFrame({
            "meter_id": meter_id, "ts": grid, "kwh": filled.to_numpy(),
            "imputed": gap, "reset_adj": reset,
        }))
    out = pd.concat(frames, ignore_index=True)
    report.intervals_imputed = int(out["imputed"].sum())
    report.resets_adjusted = int(out["reset_adj"].sum())
    return out


def _reconcile(meter: pd.DataFrame, main: str, report: CleaningReport) -> None:
    """Report how far the sub-meters fall short of main. Never rescale."""
    totals = meter.groupby("meter_id")["kwh"].sum()
    main_kwh = float(totals[main])
    subs_kwh = float(totals.drop(main).sum())
    gap = 100.0 * (main_kwh - subs_kwh) / main_kwh if main_kwh else 0.0
    tol = assumptions().submeter_tolerance_pct
    report.reconciliation_gap_pct = round(gap, 3)
    report.reconciliation_ok = abs(gap) <= tol
    report.notes.append(
        f"Sub-meters sum to {subs_kwh:,.0f} kWh against {main_kwh:,.0f} kWh on "
        f"main '{main}': gap {gap:.2f}% "
        f"({'within' if report.reconciliation_ok else 'OUTSIDE'} the {tol}% "
        "tolerance). Meters are not rescaled; the gap is unmetered load.")


def _flag_holidays(production: pd.DataFrame, report: CleaningReport) -> pd.DataFrame:
    """Flag days logged as working whose output is near zero."""
    p = production.copy()
    pattern = p["shift_pattern"].fillna("").str.strip().str.lower()
    declared_off = p["is_holiday"].fillna(False).astype(bool) | pattern.isin(_NON_WORKING) \
        | (pattern == "")
    day = pd.DataFrame({"date": p["date"], "units": p["units"].astype(float),
                        "off": declared_off})
    daily = day.groupby("date").agg(units=("units", "sum"), off=("off", "all"))
    working = daily[~daily["off"]]
    flagged: list = []
    if not working.empty:
        normal = working["units"].median()
        flagged = working.index[working["units"] < HOLIDAY_SHARE * normal].tolist()
    p["suspected_holiday"] = p["date"].isin(flagged)
    report.suspected_holidays = [pd.Timestamp(d).date().isoformat() for d in sorted(flagged)]
    for d in report.suspected_holidays:
        units = daily.loc[pd.Timestamp(d), "units"]
        report.notes.append(
            f"{d} is logged as a working day but produced {units:,.0f} units "
            f"(under {HOLIDAY_SHARE:.0%} of a normal working day): suspected "
            "holiday.")
    return p


def clean(raw: RawTables) -> tuple[CleanTables, CleaningReport]:
    report = CleaningReport(rows_in=len(raw.meter_15min))
    meter = _clean_meter(raw.meter_15min, report)
    report.rows_out = len(meter)
    main = main_meter(meter)
    _reconcile(meter, main, report)
    production = _flag_holidays(raw.production, report)
    weather = raw.weather.drop_duplicates("date", keep="first") \
        .sort_values("date").reset_index(drop=True)
    tariff = raw.tariff.reset_index(drop=True)
    if report.duplicates_dropped:
        report.notes.append(f"Dropped {report.duplicates_dropped} duplicate meter rows.")
    report.notes.append(
        f"Imputed {report.intervals_imputed} missing intervals in "
        f"{len(report.gaps)} gap(s); adjusted {report.resets_adjusted} meter "
        "reset(s). Flags are kept in meter_clean.")
    return CleanTables(meter=meter, tariff=tariff, production=production,
                       weather=weather, main_meter=main), report
