"""Invariants the generated dataset must hold, for any profile or target.

Everything downstream is measured against this ground truth, so if the true
bridge does not add up, no later number means anything.
"""

from __future__ import annotations

import pytest

from rng.data.generator import CAUSES, PROFILES, build, shapley

TARGET = 0.15
SEED = 7


@pytest.fixture(scope="module")
def primary():
    frames, injected, truth = build(PROFILES["primary"], TARGET, SEED)
    return frames, injected, truth


# -- the bridge ------------------------------------------------------------

def test_the_five_causes_add_up_to_the_total_change(primary):
    _, _, truth = primary
    parts = sum(truth["bridge_true"][c]["cost"] for c in (*CAUSES, "waste"))
    assert parts == pytest.approx(truth["actual"]["total_change"], abs=0.5)


def test_no_causes_at_current_level_reproduces_the_reference_bill(primary):
    """C(none) must BE the reference quarter, not merely resemble it.

    This is what makes additivity structural rather than a reconciliation
    fudge, and it is the invariant that breaks first if the counterfactual
    blending is ever changed carelessly.
    """
    _, _, truth = primary
    assert truth["counterfactuals"]["none"] == pytest.approx(
        truth["actual"]["reference_cost"], abs=0.5)


def test_waste_is_the_residual_not_a_separate_estimate(primary):
    _, _, truth = primary
    full = truth["counterfactuals"][",".join(sorted(CAUSES))]
    assert truth["bridge_true"]["waste"]["cost"] == pytest.approx(
        truth["actual"]["current_cost"] - full, abs=0.5)


def test_target_rise_is_a_parameter_not_a_constant():
    for target in (0.08, 0.15, 0.22):
        _, _, truth = build(PROFILES["primary"], target, SEED)
        assert truth["actual"]["rise_pct"] == pytest.approx(target * 100, abs=0.02)


def test_shapley_is_efficient_on_a_nonlinear_value_function():
    """Shapley must still sum to v(all) - v(none) when causes interact.

    If they did not interact the choice of attribution method would not matter.
    """
    def v(s: frozenset) -> float:
        return 10.0 * len(s) + (7.0 if {"tariff", "volume"} <= s else 0.0) \
            + (-3.0 if {"mix", "weather"} <= s else 0.0)

    values = shapley(v)
    assert sum(values.values()) == pytest.approx(
        v(frozenset(CAUSES)) - v(frozenset()))


# -- the Annex B contract --------------------------------------------------

def test_tables_carry_the_annex_b_columns(primary):
    frames, _, _ = primary
    required = {
        "meter_15min": {"meter_id", "ts", "kwh"},
        "tariff": {"valid_from", "valid_to", "period", "price_per_kwh"},
        "production": {"date", "product", "units", "shift_pattern", "is_holiday"},
        "weather": {"date", "avg_temp_c", "cooling_degree_days",
                    "heating_degree_days"},
    }
    for table, cols in required.items():
        assert cols <= set(frames[table].columns), table


def test_tariff_windows_are_contiguous_and_do_not_overlap(primary):
    """Windows are half-open [valid_from, valid_to).

    An inclusive 23:59:59 end leaves a one-second hole between windows, which a
    strict "tariff periods cover the whole period" check (Section 4.5) fails.
    Under half-open semantics one window's end IS the next one's start, so
    contiguity and non-overlap are the same assertion.
    """
    frames, _, _ = primary
    tf = frames["tariff"]
    for period in ("peak", "off_peak"):
        rows = tf[tf["period"] == period].sort_values("valid_from")
        ends = list(rows["valid_to"])[:-1]
        starts = list(rows["valid_from"])[1:]
        assert ends == starts, f"{period}: {ends} vs {starts}"


def test_tariff_covers_every_metered_interval(primary):
    frames, _, _ = primary
    tf, meter = frames["tariff"], frames["meter_15min"]
    assert tf["valid_from"].min() <= meter["ts"].min()
    assert tf["valid_to"].max() > meter["ts"].max()


def test_meets_the_section_4_3_minimums(primary):
    frames, _, truth = primary
    meters = set(frames["meter_15min"]["meter_id"].unique())
    assert "main" in meters and len(meters) >= 7, meters
    assert len(frames["weather"]) >= 180                    # two quarters
    assert truth["anomalies"]["mid_quarter_tariff_change"]  # one price change
    assert truth["anomalies"]["leak"]["kw"] > 0             # one hidden waste


# -- the deliberately injected edge cases ----------------------------------

def test_every_declared_edge_case_is_actually_in_the_data(primary):
    frames, injected, truth = primary
    meter, production = frames["meter_15min"], frames["production"]

    # sub-meters do not sum to the main meter
    main = meter[(meter["meter_id"] == "main")]["kwh"].sum()
    subs = meter[(meter["meter_id"] != "main") & (meter["kwh"] >= 0)]["kwh"].sum()
    assert 0.0 < (main - subs) / main < 0.06

    # missing intervals, and a meter reset as a negative delta
    assert sum(g["rows"] for g in injected["dropped_intervals"]) > 0
    assert (meter["kwh"] < 0).sum() == 1
    assert injected["meter_reset"]["rows"] == 1

    # a public holiday logged as a normal shift
    trap = truth["anomalies"]["mislabelled_holiday"]
    assert trap, "no mislabelled holiday injected"
    day = production[production["date"] == trap[0]["date"]]
    assert not day["is_holiday"].any()
    normal = production.groupby("date")["units"].sum().median()
    assert day["units"].sum() < 0.2 * normal

    # a mix shift that raises energy per unit with no waste behind it
    ref_mix = truth["drivers"]["reference_mix"]
    cur_mix = truth["drivers"]["current_mix"]
    intensity = truth["drivers"]["product_intensity_kwh_per_unit"]
    before = sum(ref_mix[p] * intensity[p] for p in ref_mix)
    after = sum(cur_mix[p] * intensity[p] for p in cur_mix)
    assert after > before


def test_the_leak_shows_as_night_load_that_was_not_there_before(primary):
    """R7's signal must survive into the emitted CSV, not just the model."""
    import pandas as pd

    frames, _, truth = primary
    leak = truth["anomalies"]["leak"]
    m = frames["meter_15min"]
    m = m[(m["meter_id"] == leak["meter_id"]) & (m["kwh"] >= 0)].copy()
    m["ts"] = pd.to_datetime(m["ts"])
    m["hour"] = m["ts"].dt.hour

    def night_share(part):
        night = part[part["hour"] < 5]["kwh"].median()
        day = part[(part["hour"] >= 9) & (part["hour"] < 17)]["kwh"].median()
        return night / day

    start = pd.Timestamp(leak["start"], tz="UTC")
    before = night_share(m[m["ts"] < start])
    after = night_share(m[m["ts"] >= start])
    assert after > before * 1.15, f"night share {before:.3f} -> {after:.3f}"


def test_the_holdout_is_not_a_rerun_of_the_primary():
    """A holdout that repeats the primary's shape tests nothing."""
    _, _, primary_truth = build(PROFILES["primary"], TARGET, SEED)
    cfg = PROFILES["holdout"]
    _, _, holdout = build(cfg, cfg["target_rise"], cfg["seed"])

    assert (holdout["anomalies"]["leak"]["meter_id"]
            != primary_truth["anomalies"]["leak"]["meter_id"])
    # The primary's mix shift raises energy per unit; the holdout's lowers it.
    assert (holdout["bridge_true"]["mix"]["pp"]
            * primary_truth["bridge_true"]["mix"]["pp"]) < 0


def test_a_mid_quarter_price_change_stays_in_the_tariff_effect():
    """Laying one quarter's tariff on the other's calendar must keep BOTH of
    its price windows.

    Clamping by date priced the whole reference calendar at the current
    quarter's first window, which dropped the mid-quarter rise from the tariff
    effect and handed it to volume (the cause that switches the calendar).
    """
    import pandas as pd

    from rng.data.generator import build_calendar, laid_on

    cfg = PROFILES["primary"]
    ref_cal = build_calendar(cfg["ref"][1], cfg["ref"][2], cfg["ref_holidays"])
    cur_cal = build_calendar(cfg["cur"][1], cfg["cur"][2], cfg["cur_holidays"])

    # On its own calendar the mapping is the identity.
    own = laid_on(cur_cal, len(cur_cal))
    assert list(own[::96].date) == list(cur_cal["date"])

    # On the other quarter's calendar, both current-quarter windows are used.
    days = laid_on(cur_cal, len(ref_cal))
    ts = pd.date_range(pd.Timestamp(ref_cal["date"].iloc[0]),
                       periods=len(ref_cal) * 96, freq="15min")
    prices = cfg["cur_tariff"].price_array(ts, days)
    peaks = {w[2] for w in cfg["cur_tariff"].windows}
    assert peaks <= set(prices.round(4)), "a price window was lost"
