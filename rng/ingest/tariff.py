"""Price per 15-minute interval. Owner: teammate B.

Annex B's tariff table names the PERIOD (peak | off_peak | flat) but never says
which HOURS are peak, so joining on valid_from / valid_to alone returns two
rows per timestamp. This function is the single place that ambiguity is
resolved, for our data and for the judges'.

Cases it must handle:
  1. peak_start_hour / peak_end_hour columns present -> use them.
     Absent -> the defaults in rng.config.assumptions(), and say so in the
     returned convention so it lands in the response's assumptions[].
  2. period == "flat": one price for every hour. Our generator never emits
     it; a judge's file may.
  3. valid_to written half-open (next window's start) OR inclusive
     (23:59:59). Join with `valid_from <= ts < valid_to`, treating an
     inclusive end as covering its whole final day.
  4. A timestamp no window covers: raise. Never price it at zero.

The tariff is the largest bridge term. It is priced interval by interval and
never as a daily total against an average price (CLAUDE.md section 9, #2).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from rng.config import assumptions
from rng.periods import to_utc

_DAY = pd.Timedelta(days=1)


def _period_name(value) -> str:
    """'Off-Peak', 'off peak', 'OFF_PEAK' -> 'off_peak'."""
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _half_open_end(valid_to: pd.Series) -> pd.Series:
    """An inclusive 23:59(:59) end covers its whole last day: move it to the
    following midnight. Anything else is already half-open."""
    inclusive = (valid_to.dt.hour == 23) & (valid_to.dt.minute == 59)
    return valid_to.where(~inclusive, valid_to.dt.floor("D") + _DAY)


def _windows(tariff: pd.DataFrame, start_hour: int, end_hour: int) -> pd.DataFrame:
    """One row per validity window: start, end, peak, off_peak, flat, hours."""
    t = tariff.copy()
    t["valid_from"] = to_utc(t["valid_from"]).to_numpy()
    raw_to = t["valid_to"].astype(str).str.strip()
    t["valid_to"] = to_utc(t["valid_to"]).to_numpy()
    # A bare-date end is half-open when another window starts on it, and an
    # inclusive last day otherwise.
    bare_date = (raw_to.str.fullmatch(r"\d{4}-\d{2}-\d{2}")
                 & ~t["valid_to"].isin(set(t["valid_from"]))).to_numpy()
    t.loc[bare_date, "valid_to"] = t.loc[bare_date, "valid_to"] + _DAY
    t.loc[~bare_date, "valid_to"] = _half_open_end(t.loc[~bare_date, "valid_to"])
    t["period"] = t["period"].map(_period_name)
    for col, default in (("peak_start_hour", start_hour), ("peak_end_hour", end_hour)):
        if col not in t.columns:
            t[col] = default
        t[col] = pd.to_numeric(t[col], errors="coerce").fillna(default).astype(int)

    rows = []
    for (start, end), g in t.groupby(["valid_from", "valid_to"], sort=True):
        prices = dict(zip(g["period"], g["price_per_kwh"].astype(float), strict=True))
        flat = prices.get("flat")
        peak = prices.get("peak", flat)
        off = prices.get("off_peak", flat)
        if peak is None or off is None:
            raise ValueError(
                f"tariff window {start} to {end} has neither a flat price nor "
                f"both peak and off_peak prices: {sorted(prices)}")
        rows.append({
            "start": start, "end": end, "peak": peak, "off_peak": off,
            "flat": flat is not None and "peak" not in prices,
            "start_hour": int(g["peak_start_hour"].iloc[0]),
            "end_hour": int(g["peak_end_hour"].iloc[0]),
        })
    return pd.DataFrame(rows)


def resolve_prices(tariff: pd.DataFrame, ts: pd.Series) -> tuple[np.ndarray, str]:
    """Return (price per interval aligned to `ts`, the convention applied)."""
    a = assumptions()
    has_hours = {"peak_start_hour", "peak_end_hour"} <= set(tariff.columns) and \
        tariff[["peak_start_hour", "peak_end_hour"]].notna().all().all()
    win = _windows(tariff, a.peak_start_hour, a.peak_end_hour)
    stamps = to_utc(ts).reset_index(drop=True)

    starts = win["start"].to_numpy(dtype="datetime64[ns]")
    ends = win["end"].to_numpy(dtype="datetime64[ns]")
    values = stamps.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy(dtype="datetime64[ns]")
    # Latest window starting at or before each timestamp; it must also not
    # have ended. A timestamp in no window is an error, never a zero price.
    idx = np.searchsorted(starts, values, side="right") - 1
    covered = (idx >= 0) & (values < ends[np.clip(idx, 0, None)])
    if not covered.all():
        missing = stamps[~covered]
        raise ValueError(
            f"{len(missing)} timestamps are not covered by any tariff window "
            f"(first {missing.iloc[0]}, last {missing.iloc[-1]})")

    w = win.iloc[idx].reset_index(drop=True)
    hour = stamps.dt.hour.to_numpy()
    s, e = w["start_hour"].to_numpy(), w["end_hour"].to_numpy()
    # A window like 22 -> 6 wraps past midnight.
    in_hours = np.where(s <= e, (hour >= s) & (hour < e), (hour >= s) | (hour < e))
    if not a.peak_applies_on_weekends:
        in_hours &= stamps.dt.dayofweek.to_numpy() < 5
    is_peak = in_hours & ~w["flat"].to_numpy()
    prices = np.where(is_peak, w["peak"].to_numpy(), w["off_peak"].to_numpy())

    if win["flat"].all():
        convention = "Flat tariff: one price for every hour of each validity window."
    else:
        hours = sorted({(r.start_hour, r.end_hour) for r in win.itertuples() if not r.flat})
        span = ", ".join(f"{h0:02d}:00-{h1:02d}:00" for h0, h1 in hours)
        days = "every day" if a.peak_applies_on_weekends else "weekdays only"
        source = ("the tariff file's peak_start_hour / peak_end_hour columns"
                  if has_hours else f"default peak hours ({a.source}); the "
                  "tariff file does not say which hours are peak")
        convention = f"Peak priced {span} UTC, {days}, from {source}."
    return prices.astype(float), convention
