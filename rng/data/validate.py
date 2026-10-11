"""Validation of the raw Annex B files (guide section 4.5). Owner: teammate A.

    python -m rng.data.validate data/primary [--out docs/validation_output.txt]

Six checks, on the CSVs exactly as they sit on disk -- not through the loader,
which has already coerced types and rejected rows by the time it returns:

    1. sub-meters within the stated tolerance of the main meter
    2. negative readings and meter resets
    3. timestamps regular, on the 15-minute grid, in one time zone
    4. tariff windows contiguous and covering every metered interval
    5. production days against the shift calendar
    6. row counts and the Annex B columns

PASS, WARN or FAIL per check. WARN is something real in the data that the
cleaning step has to handle and report (a gap, a reset, a holiday logged as a
working day). FAIL means the files cannot be used as they are, and the exit
code is 1.

Nothing here knows what the generator injected: it works on any directory of
Annex B files and never reads ground_truth.json.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from rng.config import Assumptions
from rng.ingest.clean import HOLIDAY_SHARE
from rng.ingest.loader import REQUIRED_COLUMNS, main_meter
from rng.periods import period_bounds, quarter_label

INTERVAL = pd.Timedelta(minutes=15)
DAY = pd.Timedelta(days=1)
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_SEVERITY = {PASS: 0, WARN: 1, FAIL: 2}
_NON_WORKING = {"", "down", "off", "closed", "none", "holiday", "shutdown"}
_TRUE = {"true", "1", "yes", "y", "t"}
_TARIFF_PERIODS = {"peak", "off_peak", "flat"}
_UTC_MARKS = {"Z": "UTC", "+00:00": "UTC", "+0000": "UTC"}
_MAX_LISTED = 12
_MIN_DAYS_FOR_A_NORMAL = 5


@dataclass
class Check:
    name: str
    status: str = PASS
    lines: list[str] = field(default_factory=list)

    def note(self, line: str) -> None:
        self.lines.append(line)

    def warn(self, line: str) -> None:
        self.status = max(self.status, WARN, key=_SEVERITY.get)
        self.lines.append(line)

    def fail(self, line: str) -> None:
        self.status = FAIL
        self.lines.append(line)


def _skipped(name: str, table: str) -> Check:
    return Check(name, FAIL, [f"Not run: {table}.csv is missing or unusable."])


def _read(data_dir: Path, table: str) -> pd.DataFrame | None:
    path = data_dir / f"{table}.csv"
    if not path.is_file():
        return None
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame.columns = [str(c).strip() for c in frame.columns]
    return frame


def _usable(frame: pd.DataFrame | None, table: str) -> bool:
    return (frame is not None and len(frame) > 0
            and all(c in frame.columns for c in REQUIRED_COLUMNS[table]))


def _stamps(text: pd.Series) -> pd.Series:
    return pd.to_datetime(text.str.strip(), utc=True, format="ISO8601",
                          errors="coerce")


def _days(text: pd.Series) -> pd.Series:
    """Calendar dates as naive midnights; unparseable -> NaT."""
    return _stamps(text).dt.tz_localize(None).dt.normalize()


def _tolerance(data_dir: Path) -> tuple[float, str]:
    """The data pack's stated tolerance, or our declared default."""
    meta = data_dir / "dataset_meta.json"
    if meta.is_file():
        stated = json.loads(meta.read_text(encoding="utf-8")).get(
            "submeter_reconciliation_tolerance_pct")
        if stated is not None:
            return float(stated), f"stated in {meta.name}"
    return Assumptions().submeter_tolerance_pct, "our default"


# --------------------------------------------------------------------------
# The meter checks
# --------------------------------------------------------------------------


def _parse_meter(raw: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        "meter_id": raw["meter_id"].str.strip(),
        "ts": _stamps(raw["ts"]),
        "kwh": pd.to_numeric(raw["kwh"], errors="coerce"),
    })


def _gap_runs(meter_id: str, missing: pd.DatetimeIndex) -> list[dict]:
    if len(missing) == 0:
        return []
    stamps = missing.to_series().reset_index(drop=True)
    run = (stamps.diff() != INTERVAL).cumsum()
    return [{"meter_id": meter_id, "from": r.iloc[0], "to": r.iloc[-1],
             "rows": len(r)} for _, r in stamps.groupby(run)]


def _check_reconciliation(meter: pd.DataFrame, tolerance: float,
                          source: str) -> Check:
    c = Check("Sub-meters against the main meter")
    good = meter.dropna(subset=["ts", "kwh"])
    good = good[good["kwh"] >= 0].drop_duplicates(["meter_id", "ts"])
    if good["meter_id"].nunique() < 2:
        c.fail("Needs a main meter and at least one sub-meter.")
        return c
    main = main_meter(good)
    # Compare only intervals where every meter has a valid reading, so a gap
    # on one sub-meter is not mistaken for unmetered load.
    wide = good.pivot(index="ts", columns="meter_id", values="kwh").dropna()
    main_kwh = float(wide[main].sum())
    subs = wide.drop(columns=main)
    subs_kwh = float(subs.to_numpy().sum())
    gap = 100.0 * (main_kwh - subs_kwh) / main_kwh
    line = (f"Main meter is '{main}' (largest total), with {subs.shape[1]} "
            f"sub-meters. Over the {len(wide):,} intervals where every meter "
            f"has a valid reading the sub-meters sum to {subs_kwh:,.0f} kWh "
            f"against {main_kwh:,.0f} kWh on main: gap {gap:.2f}%, tolerance "
            f"{tolerance}% ({source}).")
    if abs(gap) <= tolerance:
        c.note(line)
        c.note("Within tolerance. The gap is unmetered load plus measurement "
               "noise; meters are never rescaled to close it.")
    else:
        c.fail(line)
    daily = wide.resample("D").sum()
    daily_gap = 100.0 * (daily[main] - daily.drop(columns=main).sum(axis=1)) / daily[main]
    outside = int((daily_gap.abs() > tolerance).sum())
    c.note(f"Day by day the gap runs from {daily_gap.min():.2f}% to "
           f"{daily_gap.max():.2f}%; {outside} of {len(daily_gap)} days are "
           "outside tolerance.")
    return c


def _check_values(meter: pd.DataFrame) -> Check:
    c = Check("Negative readings and meter resets")
    unreadable = int(meter["kwh"].isna().sum())
    if unreadable:
        c.fail(f"{unreadable:,} kWh values are blank or not numbers.")
    negative = meter[meter["kwh"] < 0].sort_values(["ts", "meter_id"])
    if negative.empty:
        c.note("No negative readings.")
        return c
    c.warn(f"{len(negative)} negative reading(s). Consumption cannot be "
           "negative: each is a counter reset and must be replaced, not summed.")
    for row in negative.head(_MAX_LISTED).itertuples():
        c.note(f"  {row.meter_id} at {row.ts:%Y-%m-%d %H:%M} UTC: "
               f"kwh = {row.kwh:,.2f}")
    if len(negative) > _MAX_LISTED:
        c.note(f"  ... and {len(negative) - _MAX_LISTED} more")
    return c


def _check_timestamps(raw: pd.DataFrame, meter: pd.DataFrame) -> Check:
    c = Check("Timestamps regular, on the 15-minute grid, in one time zone")
    text = raw["ts"].str.strip()
    unparsed = int(meter["ts"].isna().sum())
    if unparsed:
        c.fail(f"{unparsed:,} timestamps cannot be parsed as ISO 8601.")
    marks = text.str.extract(r"(Z|[+-]\d{2}:?\d{2})$", expand=False)
    zones = marks.fillna("no zone").replace(_UTC_MARKS).value_counts()
    if list(zones.index) == ["UTC"]:
        c.note(f"All {len(text):,} timestamps carry an explicit UTC designator.")
    else:
        mix = ", ".join(f"{zone}: {n:,}" for zone, n in zones.items())
        c.fail(f"Timestamps are not all explicitly UTC ({mix}).")

    valid = meter.dropna(subset=["ts"])
    if valid.empty:
        return c
    off_grid = int((valid["ts"] != valid["ts"].dt.floor(INTERVAL)).sum())
    if off_grid:
        c.fail(f"{off_grid:,} timestamps are not on a 15-minute boundary.")
    duplicates = int(valid.duplicated(["meter_id", "ts"]).sum())
    if duplicates:
        c.warn(f"{duplicates:,} duplicate (meter_id, ts) rows.")

    grid = pd.date_range(valid["ts"].min().floor(INTERVAL),
                         valid["ts"].max().floor(INTERVAL), freq=INTERVAL)
    c.note(f"Grid runs {grid[0]:%Y-%m-%d %H:%M} to {grid[-1]:%Y-%m-%d %H:%M} "
           f"UTC: {len(grid):,} intervals expected for each of "
           f"{valid['meter_id'].nunique()} meters.")
    gaps: list[dict] = []
    for meter_id, rows in valid.groupby("meter_id", sort=True):
        present = pd.DatetimeIndex(rows["ts"].dt.floor(INTERVAL).unique())
        gaps.extend(_gap_runs(str(meter_id), grid.difference(present)))
    if not gaps:
        c.note("No missing intervals.")
        return c
    c.warn(f"{sum(g['rows'] for g in gaps):,} missing intervals in "
           f"{len(gaps)} gap(s). They must be imputed and flagged, not "
           "silently treated as zero.")
    for g in gaps[:_MAX_LISTED]:
        c.note(f"  {g['meter_id']}: {g['from']:%Y-%m-%d %H:%M} to "
               f"{g['to']:%Y-%m-%d %H:%M} UTC, {g['rows']} intervals")
    if len(gaps) > _MAX_LISTED:
        c.note(f"  ... and {len(gaps) - _MAX_LISTED} more")
    return c


# --------------------------------------------------------------------------
# Tariff
# --------------------------------------------------------------------------


def _check_tariff(raw: pd.DataFrame,
                  span: tuple[pd.Timestamp, pd.Timestamp] | None) -> Check:
    c = Check("Tariff windows contiguous and covering every metered interval")
    t = pd.DataFrame({
        "start": _stamps(raw["valid_from"]),
        "end": _stamps(raw["valid_to"]),
        "period": raw["period"].str.strip().str.lower()
                               .str.replace(r"[-\s]+", "_", regex=True),
        "price": pd.to_numeric(raw["price_per_kwh"], errors="coerce"),
    })
    if t[["start", "end"]].isna().any().any():
        c.fail("valid_from / valid_to cannot be parsed on every row.")
        return c
    # An end written as 23:59(:59) is inclusive: it covers its whole last day.
    inclusive = (t["end"].dt.hour == 23) & (t["end"].dt.minute == 59)
    t["end"] = t["end"].where(~inclusive, t["end"].dt.floor("D") + DAY)

    unknown = sorted(set(t["period"]) - _TARIFF_PERIODS)
    if unknown:
        c.fail(f"Unknown tariff period(s) {unknown}; expected peak, off_peak or flat.")
    if not (t["price"] > 0).all():
        c.fail("Every price_per_kwh must be a positive number.")
    if (t["end"] <= t["start"]).any():
        c.fail("A window ends on or before it starts.")

    windows = []
    for (start, end), rows in t.groupby(["start", "end"], sort=True):
        names = set(rows["period"])
        if rows["period"].duplicated().any():
            c.fail(f"Window from {start:%Y-%m-%d} prices a period twice.")
        if "flat" not in names and not {"peak", "off_peak"} <= names:
            c.fail(f"Window from {start:%Y-%m-%d} has {sorted(names)} only: "
                   "it needs a flat price or both peak and off_peak.")
        prices = ", ".join(f"{p} {v:g}" for p, v in zip(
            rows["period"], rows["price"], strict=True))
        windows.append((start, end, prices))
    for start, end, prices in windows:
        c.note(f"  {start:%Y-%m-%d} up to {end:%Y-%m-%d} (exclusive): {prices}")

    broken = False
    for (_, end, _), (start, _, _) in zip(windows, windows[1:], strict=False):
        if start > end:
            broken = True
            c.fail(f"Hole: nothing is priced from {end:%Y-%m-%d %H:%M} to "
                   f"{start:%Y-%m-%d %H:%M}.")
        elif start < end:
            broken = True
            c.fail(f"Overlap: two windows both price {start:%Y-%m-%d %H:%M} "
                   f"to {end:%Y-%m-%d %H:%M}.")
    if not broken:
        c.note(f"{len(windows)} window(s), contiguous, no overlaps.")

    if span is not None:
        first, last = span
        if windows[0][0] > first or windows[-1][1] <= last:
            c.fail(f"Metered data runs {first:%Y-%m-%d %H:%M} to "
                   f"{last:%Y-%m-%d %H:%M} UTC but the tariff covers "
                   f"{windows[0][0]:%Y-%m-%d %H:%M} up to "
                   f"{windows[-1][1]:%Y-%m-%d %H:%M}: some intervals have no price.")
        elif not broken:
            c.note("Every metered interval falls inside a window.")

    for start, _, _ in windows[1:]:
        quarter = quarter_label(start.date())
        if period_bounds(quarter)[0] != start.date():
            c.note(f"The price changes on {start:%Y-%m-%d}, part-way through "
                   f"{quarter}: that quarter must be priced interval by "
                   "interval, not at one rate.")
    if not {"peak_start_hour", "peak_end_hour"} <= set(raw.columns) \
            and "flat" not in set(t["period"]):
        c.note("The file does not say which hours are peak; the declared "
               "default peak window will be applied.")
    return c


# --------------------------------------------------------------------------
# Production and weather
# --------------------------------------------------------------------------


def _check_production(raw: pd.DataFrame, weather: pd.DataFrame | None,
                      meter: pd.DataFrame | None) -> Check:
    c = Check("Production days against the shift calendar")
    p = pd.DataFrame({
        "date": _days(raw["date"]),
        "units": pd.to_numeric(raw["units"], errors="coerce"),
        "pattern": raw["shift_pattern"].str.strip().str.lower(),
        "holiday": raw["is_holiday"].str.strip().str.lower().isin(_TRUE),
    })
    bad = int(p["date"].isna().sum() + p["units"].isna().sum())
    if bad:
        c.fail(f"{bad} rows have an unreadable date or units value.")
        p = p.dropna(subset=["date", "units"])
    if (p["units"] < 0).any():
        c.fail(f"{int((p['units'] < 0).sum())} rows have negative units.")
    if p.empty:
        return c
    p["off"] = p["holiday"] | p["pattern"].isin(_NON_WORKING)
    daily = p.groupby("date").agg(units=("units", "sum"),
                                  pattern=("pattern", "first"),
                                  off=("off", "all"))
    working = daily[~daily["off"]]
    c.note(f"{len(daily)} days, {raw['product'].str.strip().nunique()} "
           f"products: {len(working)} logged as working days, "
           f"{len(daily) - len(working)} as holiday or shut down.")

    energy = None
    if meter is not None:
        good = meter.dropna(subset=["ts", "kwh"])
        good = good[good["kwh"] >= 0]
        main = good[good["meter_id"] == main_meter(good)]
        energy = main.set_index("ts")["kwh"].resample("D").sum()
        energy.index = energy.index.tz_localize(None)
        days = pd.date_range(energy.index.min(), energy.index.max(), freq="D")
        absent = days.difference(daily.index)
        if len(absent):
            c.warn(f"{len(absent)} metered day(s) have no production rows "
                   f"(first {absent[0]:%Y-%m-%d}).")
        else:
            c.note("Every metered day has production rows.")
        if weather is not None:
            w_absent = days.difference(pd.DatetimeIndex(_days(weather["date"]).dropna()))
            if len(w_absent):
                c.warn(f"{len(w_absent)} metered day(s) have no weather row "
                       f"(first {w_absent[0]:%Y-%m-%d}).")
            else:
                c.note("Every metered day has a weather row.")

    # A day logged as working is compared with other days on the same shift
    # pattern, so a short Saturday shift is not mistaken for a holiday. A
    # pattern seen on too few days has no "normal" of its own -- the trap may
    # be the only day logged that way -- so it is held to all working days.
    by_pattern = working.groupby("pattern")["units"]
    established = by_pattern.transform("count") >= _MIN_DAYS_FOR_A_NORMAL
    normal = by_pattern.transform("median").where(
        established, working["units"].median())
    share = working["units"] / normal
    suspects = working[share < HOLIDAY_SHARE]
    for day, row in suspects.iterrows():
        peers = (f"'{row.pattern}' day" if established[day] else "working day")
        line = (f"{day:%Y-%m-%d} is logged as '{row.pattern}' and not a "
                f"holiday, but produced {row.units:,.0f} units, "
                f"{100 * share[day]:.0f}% of a normal {peers}")
        if energy is not None and day in energy.index:
            same = working.index[(working["pattern"] == row.pattern)
                                 | ~established[day]]
            typical = energy.reindex(same).median()
            line += (f"; the main meter used {100 * energy[day] / typical:.0f}% "
                     f"of a normal {peers}'s energy")
        c.warn(line + ". Suspected holiday logged as a normal shift.")
    if suspects.empty:
        c.note(f"No working day produced under {HOLIDAY_SHARE:.0%} of a "
               "normal day on its shift pattern.")

    if not working.empty:
        busy = daily[daily["off"] & (daily["units"] >= HOLIDAY_SHARE * working["units"].median())]
        for day, row in busy.iterrows():
            c.warn(f"{day:%Y-%m-%d} is logged as holiday or shut down but "
                   f"produced {row.units:,.0f} units.")

    if weather is not None:
        degree_days = weather[["cooling_degree_days", "heating_degree_days"]] \
            .apply(pd.to_numeric, errors="coerce")
        if (degree_days < 0).any().any():
            c.fail("Weather has negative degree days.")
        blanks = int(degree_days.isna().sum().sum())
        if blanks:
            c.warn(f"Weather has {blanks} blank or unreadable degree-day values.")
    return c


# --------------------------------------------------------------------------
# Row counts
# --------------------------------------------------------------------------


def _check_tables(frames: dict[str, pd.DataFrame | None]) -> Check:
    c = Check("Row counts and the Annex B columns")
    for table, required in REQUIRED_COLUMNS.items():
        frame = frames[table]
        if frame is None:
            c.fail(f"{table}.csv is missing.")
            continue
        missing = [col for col in required if col not in frame.columns]
        if missing:
            c.fail(f"{table}: required column(s) missing: {', '.join(missing)}.")
            continue
        line = f"{table:<12}{len(frame):>9,} rows"
        extra = [col for col in frame.columns if col not in required]
        if extra:
            line += f"   extra columns: {', '.join(extra)}"
        c.note(line)
        if len(frame) == 0:
            c.fail(f"{table}: no rows.")
        blank = {col: n for col in required
                 if (n := int((frame[col].str.strip() == "").sum()))}
        if blank:
            cells = ", ".join(f"{col} {n:,}" for col, n in blank.items())
            c.warn(f"{table}: blank cells in required columns ({cells}).")
    meter = frames["meter_15min"]
    if _usable(meter, "meter_15min"):
        per_meter = meter["meter_id"].str.strip().value_counts().sort_index()
        c.note("Rows per meter: " + ", ".join(
            f"{name} {n:,}" for name, n in per_meter.items()))
    return c


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def validate(data_dir: str | Path) -> list[Check]:
    """Run the six checks on a directory of Annex B CSVs."""
    data_dir = Path(data_dir)
    frames = {table: _read(data_dir, table) for table in REQUIRED_COLUMNS}
    ok = {table for table in REQUIRED_COLUMNS if _usable(frames[table], table)}

    meter, span = None, None
    if "meter_15min" in ok:
        meter = _parse_meter(frames["meter_15min"])
        if meter["ts"].notna().any():
            span = (meter["ts"].min(), meter["ts"].max())
        checks = [_check_reconciliation(meter, *_tolerance(data_dir)),
                  _check_values(meter),
                  _check_timestamps(frames["meter_15min"], meter)]
    else:
        checks = [_skipped(name, "meter_15min") for name in (
            "Sub-meters against the main meter",
            "Negative readings and meter resets",
            "Timestamps regular, on the 15-minute grid, in one time zone")]

    checks.append(
        _check_tariff(frames["tariff"], span) if "tariff" in ok else _skipped(
            "Tariff windows contiguous and covering every metered interval",
            "tariff"))
    checks.append(
        _check_production(frames["production"],
                          frames["weather"] if "weather" in ok else None, meter)
        if "production" in ok else _skipped(
            "Production days against the shift calendar", "production"))
    checks.append(_check_tables(frames))
    return checks


def render(data_dir: str | Path, checks: list[Check]) -> str:
    counts = Counter(c.status for c in checks)
    out = [f"Validation of {Path(data_dir).as_posix()}", ""]
    for number, check in enumerate(checks, start=1):
        out.append(f"[{check.status}] {number}. {check.name}")
        out.extend(f"       {line}" for line in check.lines)
        out.append("")
    out.append(f"Result: {counts[PASS]} passed, {counts[WARN]} with warnings, "
               f"{counts[FAIL]} failed.")
    out.append("WARN is something real in the data that the cleaning step "
               "must handle and report.")
    out.append("FAIL means the files cannot be used as they are.")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a directory of Annex B CSV files.")
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--out", type=Path, help="also write the report here")
    args = parser.parse_args(argv)

    checks = validate(args.data_dir)
    report = render(args.data_dir, checks)
    print(report, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
