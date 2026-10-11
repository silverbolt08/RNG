"""resolve_prices: the one place the peak-hours ambiguity is resolved."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from rng.config import ROOT
from rng.ingest.tariff import resolve_prices


def _ts(*values: str) -> pd.Series:
    return pd.Series(pd.to_datetime(list(values), utc=True, format="ISO8601"))


def _two_windows(**extra) -> pd.DataFrame:
    rows = [
        ("2026-04-01T00:00:00Z", "2026-07-01T00:00:00Z", "peak", 7.0),
        ("2026-04-01T00:00:00Z", "2026-07-01T00:00:00Z", "off_peak", 4.0),
        ("2026-07-01T00:00:00Z", "2026-10-01T00:00:00Z", "peak", 8.0),
        ("2026-07-01T00:00:00Z", "2026-10-01T00:00:00Z", "off_peak", 5.0),
    ]
    df = pd.DataFrame(rows, columns=["valid_from", "valid_to", "period", "price_per_kwh"])
    for col, value in extra.items():
        df[col] = value
    return df


def test_our_file_peak_offpeak_and_weekend():
    tariff = pd.read_csv(ROOT / "data" / "primary" / "tariff.csv")
    # Wed 2026-04-01 08:45 off-peak, 09:00 peak, 20:45 peak, 21:00 off-peak;
    # Sat 2026-04-04 12:00 off-peak (weekday-only peak); then the later windows.
    ts = _ts("2026-04-01T08:45Z", "2026-04-01T09:00Z", "2026-04-01T20:45Z",
             "2026-04-01T21:00Z", "2026-04-04T12:00Z", "2026-07-01T12:00Z",
             "2026-09-30T23:45Z")
    prices, convention = resolve_prices(tariff, ts)
    np.testing.assert_allclose(prices, [4.03, 7.22, 7.22, 4.03, 4.03, 7.64, 4.49])
    assert "09:00-21:00" in convention
    assert "columns" in convention


def test_every_interval_in_both_datasets_is_priced():
    for name in ("primary", "holdout"):
        tariff = pd.read_csv(ROOT / "data" / name / "tariff.csv")
        ts = pd.Series(pd.date_range("2026-04-01", "2026-10-01", freq="15min",
                                     inclusive="left", tz="UTC"))
        prices, _ = resolve_prices(tariff, ts)
        assert len(prices) == len(ts)
        assert (prices > 0).all()


def test_without_hour_columns_uses_defaults_and_says_so():
    prices, convention = resolve_prices(
        _two_windows(), _ts("2026-04-01T10:00Z", "2026-04-01T22:00Z"))
    np.testing.assert_allclose(prices, [7.0, 4.0])   # defaults are 09-21
    assert "default" in convention


def test_hour_columns_override_defaults():
    tariff = _two_windows(peak_start_hour=18, peak_end_hour=22)
    prices, _ = resolve_prices(tariff, _ts("2026-04-01T10:00Z", "2026-04-01T19:00Z"))
    np.testing.assert_allclose(prices, [4.0, 7.0])


def test_flat_tariff():
    tariff = pd.DataFrame({
        "valid_from": ["2026-04-01T00:00:00Z"], "valid_to": ["2026-10-01T00:00:00Z"],
        "period": ["flat"], "price_per_kwh": [6.5],
    })
    prices, convention = resolve_prices(
        tariff, _ts("2026-04-01T03:00Z", "2026-04-01T12:00Z", "2026-04-04T12:00Z"))
    np.testing.assert_allclose(prices, [6.5, 6.5, 6.5])
    assert convention.startswith("Flat")


def test_inclusive_end_covers_last_day():
    tariff = _two_windows()
    tariff["valid_to"] = tariff["valid_to"].map(
        {"2026-07-01T00:00:00Z": "2026-06-30T23:59:59Z",
         "2026-10-01T00:00:00Z": "2026-09-30T23:59:59Z"})
    prices, _ = resolve_prices(tariff, _ts("2026-06-30T23:59:59.5Z", "2026-06-30T23:45Z",
                                           "2026-07-01T00:00Z", "2026-09-30T23:45Z"))
    np.testing.assert_allclose(prices, [4.0, 4.0, 5.0, 5.0])


def test_bare_date_ends():
    half_open = _two_windows()
    half_open["valid_from"] = half_open["valid_from"].str[:10]
    half_open["valid_to"] = half_open["valid_to"].str[:10]
    prices, _ = resolve_prices(half_open, _ts("2026-06-30T23:45Z", "2026-07-01T00:00Z"))
    np.testing.assert_allclose(prices, [4.0, 5.0])

    inclusive = half_open.copy()
    inclusive["valid_to"] = inclusive["valid_to"].map(
        {"2026-07-01": "2026-06-30", "2026-10-01": "2026-09-30"})
    prices, _ = resolve_prices(inclusive, _ts("2026-06-30T23:45Z", "2026-09-30T23:45Z"))
    np.testing.assert_allclose(prices, [4.0, 5.0])


def test_period_names_are_normalised():
    tariff = _two_windows()
    tariff["period"] = tariff["period"].map({"peak": "Peak", "off_peak": "Off-Peak"})
    prices, _ = resolve_prices(tariff, _ts("2026-04-01T10:00Z", "2026-04-01T22:00Z"))
    np.testing.assert_allclose(prices, [7.0, 4.0])


def test_uncovered_timestamp_raises():
    with pytest.raises(ValueError, match="not covered"):
        resolve_prices(_two_windows(), _ts("2026-04-01T10:00Z", "2026-10-01T00:00Z"))
    with pytest.raises(ValueError, match="not covered"):
        resolve_prices(_two_windows(), _ts("2026-03-31T23:45Z"))


def test_output_is_positional_not_index_aligned():
    ts = _ts("2026-04-01T22:00Z", "2026-04-01T10:00Z")
    ts.index = [10, 3]
    prices, _ = resolve_prices(_two_windows(), ts)
    np.testing.assert_allclose(prices, [4.0, 7.0])
