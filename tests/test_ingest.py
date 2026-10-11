"""Loader and cleaning: the data path from CSV to CleanTables."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from rng.config import ROOT
from rng.ingest.clean import clean
from rng.ingest.loader import RawTables, load_csv, load_dir, main_meter
from rng.ingest.tariff import resolve_prices

DATA = ROOT / "data"


def _write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("name", ["primary", "holdout"])
def test_load_dir_reads_both_datasets(name):
    raw = load_dir(DATA / name)
    assert raw.rows_rejected == 0
    assert raw.rows_loaded == sum(len(f) for f in (
        raw.meter_15min, raw.tariff, raw.production, raw.weather))
    assert str(raw.meter_15min["ts"].dt.tz) == "UTC"
    assert raw.meter_15min["meter_id"].nunique() >= 7
    # The loaded tariff still prices every meter timestamp.
    prices, _ = resolve_prices(raw.tariff, raw.meter_15min["ts"])
    assert (prices > 0).all()


def test_main_meter_is_found_by_size_not_name():
    meter = pd.DataFrame({"meter_id": ["a", "site", "b", "site", "a"],
                          "kwh": [3.0, 5.0, 1.0, 5.0, -50.0]})
    assert main_meter(meter) == "site"


def test_extra_and_reordered_columns_are_accepted(tmp_path):
    path = _write(tmp_path, "m.csv", """
kwh,notes,TS,meter_id
1.5,x,2026-04-01T00:00:00Z,m1
2.5,y,2026-04-01T00:15:00Z,m1
""")
    result = load_csv(path, "meter_15min")
    assert list(result.frame.columns) == ["meter_id", "ts", "kwh"]
    assert result.rows_loaded == 2 and result.rows_rejected == 0
    assert any("notes" in m for m in result.messages)


def test_missing_required_column_is_rejected(tmp_path):
    path = _write(tmp_path, "m.csv", """
meter_id,ts
m1,2026-04-01T00:00:00Z
""")
    with pytest.raises(ValueError, match="kwh"):
        load_csv(path, "meter_15min")


def test_bad_rows_are_counted_not_dropped_silently(tmp_path):
    path = _write(tmp_path, "m.csv", """
meter_id,ts,kwh
m1,2026-04-01T00:00:00Z,1.0
m1,not-a-time,2.0
m1,2026-04-01T00:30:00Z,abc
,2026-04-01T00:45:00Z,1.0
m1,2026-04-01T01:00:00Z,
m1,2026-04-01T01:15:00Z,-3.0
""")
    result = load_csv(path, "meter_15min")
    # Negative kWh is a reset for clean() to fix, not a load error.
    assert result.rows_loaded == 2
    assert result.rows_rejected == 4
    assert any("rejected 4" in m for m in result.messages)


def test_production_types_and_nullable_columns(tmp_path):
    path = _write(tmp_path, "p.csv", """
date,product,units,shift_pattern,is_holiday
2026-04-01,a,10,2-shift,False
2026-04-02,a,0,,
2026-04-03,a,12.5,2-shift,False
2026-04-04,a,7,1-shift,maybe
2026-04-05,a,9,1-shift,TRUE
""")
    result = load_csv(path, "production")
    frame = result.frame
    assert result.rows_rejected == 2          # fractional units, unreadable flag
    assert frame["units"].tolist() == [10, 0, 9]
    assert frame["is_holiday"].tolist() == [False, False, True]
    assert pd.isna(frame["shift_pattern"].iloc[1])
    assert frame["date"].iloc[0] == pd.Timestamp("2026-04-01")


def test_tariff_optional_hour_columns_kept_only_when_present(tmp_path):
    with_hours = load_csv(DATA / "primary" / "tariff.csv", "tariff").frame
    assert {"peak_start_hour", "peak_end_hour"} <= set(with_hours.columns)
    path = _write(tmp_path, "t.csv", """
valid_from,valid_to,period,price_per_kwh
2026-04-01T00:00:00Z,2026-10-01T00:00:00Z,Off-Peak,4.0
2026-04-01T00:00:00Z,2026-10-01T00:00:00Z,Peak,7.0
""")
    frame = load_csv(path, "tariff").frame
    assert "peak_start_hour" not in frame.columns
    assert frame["period"].tolist() == ["off_peak", "peak"]
    prices, convention = resolve_prices(
        frame, pd.Series(pd.to_datetime(["2026-04-01T10:00Z"], utc=True)))
    np.testing.assert_allclose(prices, [7.0])
    assert "default" in convention


def test_load_dir_reports_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="weather.csv"):
        for t in ("meter_15min", "tariff", "production"):
            (tmp_path / f"{t}.csv").write_text("x\n", encoding="utf-8")
        load_dir(tmp_path)


# --------------------------------------------------------------------------
# clean()
# --------------------------------------------------------------------------

def _raw(meter: pd.DataFrame, production: pd.DataFrame | None = None) -> RawTables:
    if production is None:
        production = pd.DataFrame({
            "date": pd.to_datetime(["2026-04-01"]), "product": ["a"],
            "units": pd.array([10], dtype="Int64"), "shift_pattern": ["2-shift"],
            "is_holiday": pd.array([False], dtype="boolean")})
    weather = pd.DataFrame({"date": pd.to_datetime(["2026-04-01"]), "avg_temp_c": [20.0],
                            "cooling_degree_days": [2.0], "heating_degree_days": [0.0]})
    tariff = pd.DataFrame({"valid_from": pd.to_datetime(["2026-04-01"], utc=True),
                           "valid_to": pd.to_datetime(["2026-05-01"], utc=True),
                           "period": ["flat"], "price_per_kwh": [5.0]})
    return RawTables(meter, tariff, production, weather)


def _meter(n: int = 3 * 96) -> pd.DataFrame:
    ts = pd.date_range("2026-04-01", periods=n, freq="15min", tz="UTC")
    sub = pd.DataFrame({"meter_id": "sub", "ts": ts, "kwh": 1.0})
    main = pd.DataFrame({"meter_id": "main", "ts": ts, "kwh": 1.02})
    return pd.concat([main, sub], ignore_index=True)


def test_primary_report_lists_three_gaps_and_one_reset():
    tables, report = clean(load_dir(DATA / "primary"))
    assert len(report.gaps) == 3
    assert report.resets_adjusted == 1
    assert report.reconciliation_ok
    assert report.suspected_holidays            # the mislabelled working day
    m = tables.meter
    assert (m["kwh"] >= 0).all() and m["kwh"].notna().all()
    assert int(m["imputed"].sum()) == sum(g["rows"] for g in report.gaps)
    # Every meter now sits on the same regular grid.
    per_meter = m.groupby("meter_id")["ts"].agg(["count", "min", "max"])
    assert per_meter["count"].nunique() == 1
    assert (m.groupby("meter_id")["ts"].diff().dropna() == pd.Timedelta("15min")).all()


def test_holdout_is_cleaned_too():
    tables, report = clean(load_dir(DATA / "holdout"))
    assert report.gaps and report.resets_adjusted == 1
    assert tables.production["suspected_holiday"].any()


def test_gap_is_imputed_and_flagged():
    meter = _meter()
    drop = (meter["meter_id"] == "sub") & meter["ts"].between(
        pd.Timestamp("2026-04-02T02:00Z"), pd.Timestamp("2026-04-02T03:45Z"))
    tables, report = clean(_raw(meter[~drop]))
    sub = tables.meter[tables.meter["meter_id"] == "sub"]
    assert sub["imputed"].sum() == 8
    assert report.gaps == [{"meter_id": "sub", "from": "2026-04-02T02:00:00+00:00",
                            "to": "2026-04-02T03:45:00+00:00", "rows": 8}]
    np.testing.assert_allclose(sub["kwh"], 1.0)


def test_reset_is_replaced_from_neighbours():
    meter = _meter()
    meter.loc[10, "kwh"] = -4000.0
    tables, report = clean(_raw(meter))
    row = tables.meter[(tables.meter["meter_id"] == "main")].iloc[10]
    assert row["reset_adj"] and not row["imputed"]
    assert row["kwh"] == pytest.approx(1.02)
    assert report.resets_adjusted == 1


def test_duplicates_are_dropped_and_counted():
    meter = _meter()
    tables, report = clean(_raw(pd.concat([meter, meter.iloc[:5]], ignore_index=True)))
    assert report.duplicates_dropped == 5
    assert len(tables.meter) == len(meter)


def test_meters_are_never_rescaled_to_agree():
    meter = _meter()
    meter.loc[meter["meter_id"] == "main", "kwh"] = 2.0     # subs cover only half
    tables, report = clean(_raw(meter))
    assert not report.reconciliation_ok
    assert report.reconciliation_gap_pct == pytest.approx(50.0)
    np.testing.assert_allclose(tables.meter.loc[tables.meter["meter_id"] == "sub", "kwh"], 1.0)
    assert tables.main_meter == "main"


def test_near_zero_working_day_is_a_suspected_holiday():
    dates = pd.to_datetime(["2026-04-01", "2026-04-02", "2026-04-03", "2026-04-04",
                            "2026-04-05"])
    production = pd.DataFrame({
        "date": dates, "product": "a",
        "units": pd.array([100, 98, 3, 0, 101], dtype="Int64"),
        "shift_pattern": ["2-shift", "2-shift", "2-shift", "down", "1-shift"],
        "is_holiday": pd.array([False, False, False, False, False], dtype="boolean")})
    tables, report = clean(_raw(_meter(), production))
    assert report.suspected_holidays == ["2026-04-03"]
    assert tables.production["suspected_holiday"].tolist() == [False, False, True, False, False]
