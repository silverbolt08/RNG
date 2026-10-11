"""Analytics core: baseline, bridge, waste, what-if. Owner: teammate C.

The four ways this breaks (CLAUDE.md section 9) each have a test here:
  1. baseline fitted on both quarters    -> waste collapses
  2. daily totals x an average price     -> a different tariff term
  3. waste estimated instead of residual -> the bridge stops adding up
  4. leaks searched before the bridge    -> impossible by signature; and a
     bridge with no waste blames nothing

Tests may read the generator's answer key to check results; the code under
rng/analysis may not, and tests/test_no_generator_import.py enforces that.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from rng.analysis import decompose as D
from rng.analysis.baseline import daily_frame, fit_baseline, fit_frame
from rng.analysis.waste import detect_leaks, find_idle_load
from rng.analysis.whatif import scale_tariff, what_if_tariff
from rng.api.schemas import Cause
from rng.ingest.clean import clean
from rng.ingest.loader import load_dir
from rng.periods import available_periods, previous_period

ROOT = Path(__file__).resolve().parents[1]
DATASETS = ("primary", "holdout")


def _truth(name: str) -> dict:
    return json.loads((ROOT / "data" / name / "ground_truth.json").read_text())


def _periods(tables) -> tuple[str, str]:
    """(current, reference) read from the data, never hard-coded."""
    current = available_periods(tables.meter["ts"])[-1]
    return current, previous_period(current)


@pytest.fixture(scope="module", params=DATASETS)
def case(request):
    tables, _ = clean(load_dir(ROOT / "data" / request.param))
    period, ref = _periods(tables)
    model = fit_baseline(tables, ref)
    bridge = D.decompose(tables, period, ref, model)
    return {"name": request.param, "tables": tables, "period": period, "ref": ref,
            "model": model, "bridge": bridge, "truth": _truth(request.param)}


@pytest.fixture(scope="module")
def primary():
    tables, _ = clean(load_dir(ROOT / "data" / "primary"))
    period, ref = _periods(tables)
    model = fit_baseline(tables, ref)
    return tables, period, ref, model


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------

def test_baseline_is_fitted_on_the_reference_quarter_and_reports_r2(case):
    m = case["model"]
    assert m.fitted_on == case["ref"]
    assert m.n_days == len(daily_frame(case["tables"], case["ref"]))
    assert 0.9 < m.r2 <= 1.0
    assert case["bridge"].r2 > 0.9


def test_decompose_refuses_a_baseline_fitted_on_another_period(primary):
    tables, period, ref, model = primary
    with pytest.raises(ValueError, match="fitted on"):
        D.decompose(tables, period, ref, replace(model, fitted_on=period))


def test_fitting_on_both_quarters_shrinks_waste(primary):
    """Section 9 #1: fitted on both quarters, the leak becomes 'normal'."""
    tables, period, ref, model = primary
    setup = D._setup(tables, period, ref)
    proper = D._bridge(setup, model, 0)
    both = pd.concat([setup.frames["ref"], setup.frames["cur"]])
    naive = fit_frame(both, ref, setup.names)
    effects, _ = D._split(setup, naive)
    naive_pp = 100.0 * effects[Cause.WASTE] / setup.actual["ref"]
    assert proper.waste_pp > 1.0
    assert naive_pp < 0.5 * proper.waste_pp


# --------------------------------------------------------------------------
# Bridge
# --------------------------------------------------------------------------

def test_bridge_adds_up_exactly(case):
    b = case["bridge"]
    b.assert_adds_up()
    assert set(b.effects) == set(Cause)
    assert sum(b.effects.values()) == pytest.approx(b.total_change, abs=1e-6)


def test_bridge_total_is_the_measured_change(case):
    actual = case["truth"]["actual"]
    b = case["bridge"]
    assert b.reference_cost == pytest.approx(actual["reference_cost"], rel=1e-4)
    assert b.current_cost == pytest.approx(actual["current_cost"], rel=1e-4)
    assert b.total_pct == pytest.approx(actual["rise_pct"], abs=0.01)


def test_waste_is_the_residual(case):
    """Section 9 #3: waste = actual - C(all), never a separate estimate."""
    b = case["bridge"]
    c_all = b.details["counterfactuals"][D.subset_label(D.MODELLED_CAUSES)]
    assert b.effects[Cause.WASTE] == pytest.approx(b.current_cost - c_all, abs=0.01)
    assert b.details["counterfactuals"]["none"] == pytest.approx(b.reference_cost, abs=0.01)


def test_interval_pricing_differs_from_daily_average_pricing(primary):
    """Section 9 #2: a daily total at an average price gets tariff wrong."""
    tables, period, ref, model = primary
    setup = D._setup(tables, period, ref)
    interval = D._bridge(setup, model, 0)
    flat = replace(setup, eff_price={k: np.full_like(v, v.mean())
                                     for k, v in setup.eff_price.items()})
    effects, _ = D._split(flat, model)
    flat_pp = 100.0 * effects[Cause.TARIFF] / setup.actual["ref"]
    assert abs(flat_pp - interval.pct_points(Cause.TARIFF)) > 0.05


def test_every_cause_has_a_range_around_its_point(case):
    b = case["bridge"]
    for item in b.to_items():
        low, high = item.range
        assert low <= item.pct_points <= high
    assert set(b.ranges_pp) == set(Cause)
    assert all(lo < hi for lo, hi in b.ranges_pp.values())


def test_mix_shift_is_blamed_on_mix_with_the_right_sign(case):
    """The trap: kWh per unit moves because of the mix, not a machine."""
    true_mix = case["truth"]["bridge_true"]["mix"]["pp"]
    est = case["bridge"].pct_points(Cause.PRODUCT_MIX)
    assert np.sign(est) == np.sign(true_mix)
    assert abs(est) > 1.0


def test_split_is_close_to_truth_where_the_data_identifies_it(case):
    """Totals that do not depend on separating standby from per-unit energy.

    Every day of one shift pattern has the same output in these datasets, so
    per-shift standby and per-unit energy cannot be told apart and the model
    books standby as per-unit. When output per shift rises (holdout: ~17%),
    some of the residual moves into production -- hence 2 pp, not 1.
    Tariff and volume are summed because the generator prices the reference
    calendar at the first current-quarter price, which moves part of a
    mid-quarter tariff change into its volume term.
    """
    b, truth = case["bridge"], case["truth"]["bridge_true"]
    est_tv = b.pct_points(Cause.TARIFF) + b.pct_points(Cause.VOLUME) \
        + b.pct_points(Cause.PRODUCT_MIX)
    true_tv = truth["tariff"]["pp"] + truth["volume"]["pp"] + truth["mix"]["pp"]
    assert est_tv == pytest.approx(true_tv, abs=2.0)
    assert b.pct_points(Cause.WEATHER) == pytest.approx(truth["weather"]["pp"], abs=0.6)
    assert b.waste_pp == pytest.approx(truth["waste"]["pp"], abs=1.2)


def test_shapley_is_order_free_where_sequential_is_not(primary):
    tables, period, ref, model = primary
    spread = D.attribution_spread(tables, period, ref, model)
    widest = max(v["sequential_max"] - v["sequential_min"] for v in spread.values())
    assert widest > 0.1
    for v in spread.values():
        assert v["sequential_min"] - 1e-9 <= v["shapley"] <= v["sequential_max"] + 1e-9


# --------------------------------------------------------------------------
# Idle load and leaks
# --------------------------------------------------------------------------

def test_idle_load_covers_every_sub_meter(case):
    tables = case["tables"]
    idle = find_idle_load(tables, case["period"])
    subs = set(tables.meter["meter_id"]) - {tables.main_meter}
    assert set(idle) == subs
    for s in idle.values():
        assert s.idle_hours > 0 and s.idle_kwh >= 0 and s.cost >= 0
        assert s.median_idle_kw >= 0 and s.night_share >= 0


def test_detect_leaks_finds_the_injected_leak_and_nothing_else(case):
    tables, b = case["tables"], case["bridge"]
    leak = case["truth"]["anomalies"]["leak"]
    findings = detect_leaks(tables, b, find_idle_load(tables, case["period"]))
    assert [f.asset for f in findings] == [leak["meter_id"]]
    f = findings[0]
    assert f.kwh_per_year == pytest.approx(leak["kw"] * 8760, rel=0.1)
    found = pd.Timestamp(f.detected_from)
    assert abs((found - pd.Timestamp(leak["start"])).days) <= 2
    assert f.evidence_refs


def _without_leak(tables, leak: dict):
    """The same data with the injected leak taken back out of both meters."""
    meter = tables.meter.copy()
    after = meter["ts"] >= pd.Timestamp(leak["start"], tz="UTC")
    hit = after & meter["meter_id"].isin([leak["meter_id"], tables.main_meter])
    meter.loc[hit, "kwh"] -= leak["kw"] * 0.25
    return replace(tables, meter=meter)


def test_no_leak_means_no_finding(case):
    tables = _without_leak(case["tables"], case["truth"]["anomalies"]["leak"])
    b = D.decompose(tables, case["period"], case["ref"], case["model"])
    assert b.waste_pp < case["bridge"].waste_pp - 0.5
    assert detect_leaks(tables, b, find_idle_load(tables, case["period"])) == []


def test_no_waste_blames_no_machine(case):
    """The gate: with the residual gone, nothing is flagged even though the
    meter data still contains the leak."""
    b = case["bridge"]
    effects = dict(b.effects)
    effects[Cause.TARIFF] += effects[Cause.WASTE]
    effects[Cause.WASTE] = 0.0
    no_waste = replace(b, effects=effects, ranges_pp={})
    tables = case["tables"]
    assert detect_leaks(tables, no_waste, find_idle_load(tables, case["period"])) == []


def test_leak_search_needs_a_bridge():
    """Section 9 #4: the order of attribution is enforced by the signature."""
    params = inspect.signature(detect_leaks).parameters
    assert list(params) == ["clean", "bridge", "idle"]
    assert all(p.default is inspect.Parameter.empty for p in params.values())


# --------------------------------------------------------------------------
# What-if
# --------------------------------------------------------------------------

def test_lower_tariff_moves_the_tariff_term_and_little_else(primary):
    tables, period, ref, model = primary
    w = what_if_tariff(tables, period, ref, model, 0.9)
    w.base.assert_adds_up()
    w.scenario.assert_adds_up()
    assert w.delta_pp[Cause.TARIFF] < -5.0
    assert w.scenario.reference_cost == pytest.approx(w.base.reference_cost)
    for cause in (Cause.VOLUME, Cause.PRODUCT_MIX, Cause.WEATHER, Cause.WASTE):
        assert abs(w.delta_pp[cause]) < 0.1 * abs(w.delta_pp[Cause.TARIFF])


def test_scaling_a_window_that_spans_both_periods_leaves_the_reference_alone():
    tariff = pd.DataFrame({
        "valid_from": ["2026-04-01T00:00:00Z"] * 2,
        "valid_to": ["2026-10-01T00:00:00Z"] * 2,
        "period": ["peak", "off_peak"], "price_per_kwh": [8.0, 4.0]})
    out = scale_tariff(tariff, "2026-Q3", 0.5)
    ref = out[out["valid_from"] < pd.Timestamp("2026-07-01", tz="UTC")]
    cur = out[out["valid_from"] >= pd.Timestamp("2026-07-01", tz="UTC")]
    assert sorted(ref["price_per_kwh"]) == [4.0, 8.0]
    assert sorted(cur["price_per_kwh"]) == [2.0, 4.0]
