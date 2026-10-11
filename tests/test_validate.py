"""The validation script finds what is wrong with the raw files.

The validator never reads ground_truth.json; these tests do, to prove that
what it reports is exactly what the generator injected, on both datasets.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

from rng.data.validate import FAIL, PASS, WARN, main, render, validate

ROOT = Path(__file__).resolve().parents[1]
DATASETS = ("primary", "holdout")
TABLES = ("meter_15min", "tariff", "production", "weather")


def _by_name(checks) -> dict[str, object]:
    return {c.name.split(",")[0].split(" ")[0].lower(): c for c in checks}


@pytest.fixture(scope="module", params=DATASETS)
def dataset(request):
    data_dir = ROOT / "data" / request.param
    truth = json.loads((data_dir / "ground_truth.json").read_text(encoding="utf-8"))
    checks = validate(data_dir)
    return checks, render(data_dir, checks), truth["anomalies"]


def test_all_six_checks_run_and_none_fails(dataset):
    checks, _, _ = dataset
    assert len(checks) == 6
    assert FAIL not in {c.status for c in checks}


def test_it_reports_the_meter_reset(dataset):
    checks, report, injected = dataset
    reset = injected["meter_reset"]
    check = _by_name(checks)["negative"]
    assert check.status == WARN
    stamp = pd.Timestamp(reset["ts"]).strftime("%Y-%m-%d %H:%M")
    assert f"{reset['meter_id']} at {stamp}" in report


def test_it_reports_every_gap_and_no_others(dataset):
    checks, report, injected = dataset
    gaps = injected["dropped_intervals"]
    check = _by_name(checks)["timestamps"]
    assert check.status == WARN
    total = sum(g["rows"] for g in gaps)
    assert f"{total:,} missing intervals in {len(gaps)} gap(s)" in report
    for gap in gaps:
        start = pd.Timestamp(gap["from"]).strftime("%Y-%m-%d %H:%M")
        assert f"{gap['meter_id']}: {start}" in report
        assert f"{gap['rows']} intervals" in report


def test_it_reports_the_holiday_logged_as_a_working_day(dataset):
    checks, report, injected = dataset
    check = _by_name(checks)["production"]
    assert check.status == WARN
    flagged = [line for line in check.lines if "Suspected holiday" in line]
    assert len(flagged) == len(injected["mislabelled_holiday"])
    for trap in injected["mislabelled_holiday"]:
        assert any(trap["date"] in line and trap["logged_as"] in line
                   for line in flagged)


def test_submeters_reconcile_within_the_stated_tolerance(dataset):
    checks, report, _ = dataset
    assert _by_name(checks)["sub-meters"].status == PASS
    assert "stated in dataset_meta.json" in report


def test_the_mid_quarter_tariff_change_is_pointed_out(dataset):
    checks, report, injected = dataset
    assert _by_name(checks)["tariff"].status == PASS
    assert f"price changes on {injected['mid_quarter_tariff_change']}" in report


# -- broken files must fail, not pass quietly --------------------------------

@pytest.fixture
def copy(tmp_path):
    for table in TABLES:
        shutil.copy(ROOT / "data" / "primary" / f"{table}.csv", tmp_path)
    return tmp_path


def _rewrite(data_dir: Path, table: str, change) -> None:
    path = data_dir / f"{table}.csv"
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    change(frame).to_csv(path, index=False)


def test_a_hole_in_the_tariff_fails(copy):
    # Drop the middle window: part of the quarter then has no price.
    _rewrite(copy, "tariff", lambda t: t[~t["valid_from"].str.startswith("2026-07")])
    check = _by_name(validate(copy))["tariff"]
    assert check.status == FAIL
    assert any("Hole" in line for line in check.lines)


def test_a_tariff_that_stops_before_the_meter_data_fails(copy):
    _rewrite(copy, "tariff", lambda t: t[~t["valid_from"].str.startswith("2026-08")])
    check = _by_name(validate(copy))["tariff"]
    assert check.status == FAIL
    assert any("no price" in line for line in check.lines)


def test_an_inclusive_tariff_end_is_not_reported_as_a_hole(copy):
    def inclusive(t):
        t["valid_to"] = (pd.to_datetime(t["valid_to"]) - pd.Timedelta(seconds=1)) \
            .dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        return t
    _rewrite(copy, "tariff", inclusive)
    assert _by_name(validate(copy))["tariff"].status == PASS


def test_a_missing_required_column_fails_and_names_it(copy):
    _rewrite(copy, "weather", lambda w: w.drop(columns=["cooling_degree_days"]))
    check = _by_name(validate(copy))["row"]
    assert check.status == FAIL
    assert any("cooling_degree_days" in line for line in check.lines)


def test_extra_and_reordered_columns_are_accepted(copy):
    def shuffle(m):
        m["site"] = "plant-1"
        return m[["site", "kwh", "ts", "meter_id"]]
    _rewrite(copy, "meter_15min", shuffle)
    checks = validate(copy)
    assert FAIL not in {c.status for c in checks}


def test_mixed_time_zones_fail(copy):
    def local_time(m):
        m.loc[m.index[:50], "ts"] = m.loc[m.index[:50], "ts"].str.replace(
            "Z", "+05:30", regex=False)
        return m
    _rewrite(copy, "meter_15min", local_time)
    assert _by_name(validate(copy))["timestamps"].status == FAIL


def test_submeters_far_from_main_fail(copy):
    def halve_main(m):
        main = m["meter_id"] == "main"
        m.loc[main, "kwh"] = (m.loc[main, "kwh"].astype(float) * 2).astype(str)
        return m
    _rewrite(copy, "meter_15min", halve_main)
    assert _by_name(validate(copy))["sub-meters"].status == FAIL


def test_meter_names_are_not_assumed(copy):
    """Rename every meter: the main supply is found by size, not by name."""
    def rename(m):
        m["meter_id"] = "M-" + m["meter_id"].str.upper().str[::-1]
        return m
    _rewrite(copy, "meter_15min", rename)
    checks = validate(copy)
    assert _by_name(checks)["sub-meters"].status == PASS
    assert "M-NIAM" in render(copy, checks)


def test_a_missing_file_fails_without_crashing(copy):
    (copy / "meter_15min.csv").unlink()
    checks = validate(copy)
    assert len(checks) == 6
    assert _by_name(checks)["row"].status == FAIL


def test_cli_writes_the_report_and_exits_zero_on_usable_data(tmp_path, capsys):
    out = tmp_path / "validation_output.txt"
    assert main([str(ROOT / "data" / "primary"), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == capsys.readouterr().out


def test_cli_exits_nonzero_when_a_check_fails(copy, capsys):
    (copy / "tariff.csv").unlink()
    assert main([str(copy)]) == 1
