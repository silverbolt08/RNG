"""Synthetic factory energy data generator (Annex B schema).

Simulates a factory from physical drivers -- production volume, product mix,
weather and tariff -- and emits the four Annex B tables, a dataset_meta.json
describing the pack, and a ground_truth.json recording the true cost
decomposition plus the location of every injected anomaly.

Ground truth is produced by running the *same* 16-subset counterfactual the
analyser runs, but evaluated with the true physical model instead of a fitted
baseline. Generator and analyser share the definition of C(S) -- they must, or
the comparison is meaningless. They must never share the model.

Nothing under rng/analysis/ may import this module; see
tests/test_no_generator_import.py.

Run:
    python -m rng.data.generator --profile primary --out data/primary
    python -m rng.data.generator --profile holdout --out data/holdout
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from itertools import combinations
from math import factorial
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Fixed structure
# --------------------------------------------------------------------------

CAUSES = ("tariff", "volume", "mix", "weather")
INTERVALS_PER_DAY = 96
STEP_HOURS = 0.25
HOURS = np.arange(INTERVALS_PER_DAY) * STEP_HOURS

SUBMETERS = ("furnace", "line_1", "line_2", "line_3",
             "hvac", "chillers", "compressors", "lighting")
METERS = ("main",) + SUBMETERS

# kWh at the furnace per unit produced. The spread across families is what
# makes a mix shift raise energy per unit with no waste at all.
PRODUCT_INTENSITY = {"widget_a": 1.00, "widget_b": 2.30, "alloy_core": 6.40}
PRODUCTS = tuple(PRODUCT_INTENSITY)

SHIFT_FACTOR = {"3-shift": 1.30, "2-shift": 1.00, "1-shift": 0.45,
                "skeleton": 0.05, "down": 0.0}

# Plant ratings. Chosen to land total demand near 800 kW average.
FURNACE_STANDBY_KW = 40.0
LINE_INTENSITY = (0.55, 0.48, 0.42)     # kWh per unit, per line
LINE_STANDBY_KW = (28.0, 26.0, 22.0)
COMPRESSOR_KW = 130.0
LIGHTING_KW = 48.0
CHILLER_KWH_PER_UNIT = 0.35
CHILLER_KW_PER_DEGC = 1.4
UNMETERED_KW = 18.0

# Piecewise cooling response -- the part a linear beta_cdd * CDD term cannot
# represent. Deliberate: a small honest residual beats a suspicious R2 of 0.99.
HVAC_BASE_KW = 55.0
COOL_ONSET_C = 20.0
COOL_KINK_C = 27.0
COOL_KW_PER_DEGC = 4.0
COOL_KW_PER_DEGC_ABOVE_KINK = 5.80      # 1.45x the lower slope
HEAT_BALANCE_C = 15.0
HEAT_KW_PER_DEGC = 5.2

INTRADAY_TEMP_AMPLITUDE_C = 5.0
DEGREE_DAY_BASE_C = 18.0

SIGMA_SUBMETER = 0.012                  # per-interval measurement noise
SIGMA_MAIN = 0.006


# --------------------------------------------------------------------------
# Tariff
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Tariff:
    """Validity windows with a peak/off-peak price each.

    Annex B gives tariff.csv a `period` column but never says which hours are
    peak. We publish the convention as extra columns and in dataset_meta.json,
    and the loader falls back to this default for files that omit them.
    """

    windows: tuple                      # (start: date, end: date, peak, off_peak)
    peak_start_hour: int = 9
    peak_end_hour: int = 21

    def price_array(self, ts: pd.DatetimeIndex) -> np.ndarray:
        day = ts.normalize()
        peak = np.full(len(ts), self.windows[-1][2], dtype=float)
        off = np.full(len(ts), self.windows[-1][3], dtype=float)
        # Walk backwards so the earliest window wins for early timestamps and
        # anything past the final window clamps to the final price. A
        # counterfactual applies one quarter's prices to the other's calendar.
        for start, end, pk, op in reversed(self.windows):
            m = day <= pd.Timestamp(end)
            peak[m] = pk
            off[m] = op
        is_peak = ((ts.hour >= self.peak_start_hour)
                   & (ts.hour < self.peak_end_hour)
                   & (ts.dayofweek < 5))
        return np.where(is_peak, peak, off)

    def to_frame(self) -> pd.DataFrame:
        rows = []
        for start, end, pk, op in self.windows:
            for period, price in (("peak", pk), ("off_peak", op)):
                rows.append({
                    "valid_from": f"{start.isoformat()}T00:00:00Z",
                    "valid_to": f"{(end + timedelta(days=1)).isoformat()}T00:00:00Z",
                    "period": period,
                    "price_per_kwh": round(price, 5),
                    "peak_start_hour": self.peak_start_hour,
                    "peak_end_hour": self.peak_end_hour,
                })
        return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Calendar, weather, drivers
# --------------------------------------------------------------------------

def occupancy_profile(pattern: str) -> np.ndarray:
    """Fraction of plant running, per 15-minute slot."""
    if pattern == "3-shift":
        o = np.full(INTERVALS_PER_DAY, 1.0)
        o[(HOURS >= 2) & (HOURS < 5)] = 0.82
    elif pattern == "2-shift":
        o = np.full(INTERVALS_PER_DAY, 0.08)
        o[(HOURS >= 6) & (HOURS < 22)] = 1.0
    elif pattern == "1-shift":
        o = np.full(INTERVALS_PER_DAY, 0.06)
        o[(HOURS >= 6) & (HOURS < 14)] = 1.0
    elif pattern == "skeleton":
        o = np.full(INTERVALS_PER_DAY, 0.05)
        o[(HOURS >= 8) & (HOURS < 12)] = 0.22
    else:                                # down
        o = np.full(INTERVALS_PER_DAY, 0.04)
    # Soften the shift edges so the load shape is not a square wave.
    k = np.array([0.15, 0.70, 0.15])
    return np.convolve(np.r_[o[0], o, o[-1]], k, mode="same")[1:-1]


OCCUPANCY = {p: occupancy_profile(p) for p in SHIFT_FACTOR}


def build_calendar(start: date, end: date, holidays: dict) -> pd.DataFrame:
    """One row per day.

    `true_*` columns drive the physics. `shift_pattern` / `is_holiday` are what
    production.csv will claim -- they differ on the mislabelled-holiday trap.
    """
    days = pd.date_range(start, end, freq="D").date
    rows = []
    for d in days:
        dow = d.weekday()
        pattern = "down" if dow == 6 else "1-shift" if dow == 5 else "2-shift"
        is_special = d in holidays
        if is_special:
            pattern = holidays[d]["true_pattern"]
        rows.append({
            "date": d,
            "true_pattern": pattern,
            "true_is_holiday": is_special,
            "shift_pattern": (holidays[d]["logged_pattern"]
                              if is_special else pattern),
            "is_holiday": (holidays[d]["logged_holiday"]
                           if is_special else False),
        })
    return pd.DataFrame(rows)


def build_temps(cal: pd.DataFrame, seed: int, warm_shift: float,
                excursion: tuple | None) -> np.ndarray:
    """Daily mean temperature: seasonal curve + AR(1) weather noise."""
    rng = np.random.default_rng(seed)
    doy = np.array([d.timetuple().tm_yday for d in cal["date"]], dtype=float)
    seasonal = 16.0 + 10.0 * np.sin(2 * np.pi * (doy - 100.0) / 365.0)
    noise = np.zeros(len(doy))
    shock = rng.standard_normal(len(doy)) * 1.6
    for i in range(1, len(doy)):
        noise[i] = 0.72 * noise[i - 1] + shock[i]
    temps = seasonal + noise + warm_shift
    if excursion is not None:
        lo, hi, bump = excursion
        mask = np.array([lo <= d <= hi for d in cal["date"]])
        temps[mask] += bump
    return temps


@dataclass(frozen=True)
class Drivers:
    """The four causes, plus the calendar they act on.

    The calendar travels with `volume`: how much the factory ran includes how
    many working days it had. That is what makes C(no causes) identically equal
    to the reference quarter's actual bill, which is what makes the bridge add
    up exactly rather than approximately.
    """

    label: str
    calendar: pd.DataFrame
    total_units: float
    mix: tuple                          # ((product, weight), ...)
    temps: np.ndarray
    tariff: Tariff


def _resample(values: np.ndarray, n: int) -> np.ndarray:
    if len(values) == n:
        return values
    src = np.linspace(0.0, 1.0, len(values))
    return np.interp(np.linspace(0.0, 1.0, n), src, values)


def blend(ref: Drivers, cur: Drivers, active: frozenset) -> Drivers:
    """Drivers with the causes in `active` at current-quarter levels."""
    base = cur if "volume" in active else ref
    weather_src = cur if "weather" in active else ref
    return Drivers(
        label=f"C({','.join(sorted(active)) or 'none'})",
        calendar=base.calendar,
        total_units=base.total_units,
        mix=cur.mix if "mix" in active else ref.mix,
        temps=_resample(weather_src.temps, len(base.calendar)),
        tariff=cur.tariff if "tariff" in active else ref.tariff,
    )


# --------------------------------------------------------------------------
# Noise field
# --------------------------------------------------------------------------

class NoiseField:
    """Measurement noise drawn once per (meter, date, slot).

    Fixed rather than re-drawn, so every counterfactual sees the same instrument
    error and C(no causes) reproduces the reference bill to floating point.
    """

    def __init__(self, start: date, end: date, seed: int):
        dates = pd.date_range(start, end, freq="D").date
        self._pos = {d: i for i, d in enumerate(dates)}
        rng = np.random.default_rng(seed)
        self._z = {m: rng.standard_normal((len(dates), INTERVALS_PER_DAY))
                   for m in METERS}

    def rows(self, meter: str, dates) -> np.ndarray:
        return self._z[meter][[self._pos[d] for d in dates]]


# --------------------------------------------------------------------------
# The physical simulation
# --------------------------------------------------------------------------

def _apportion(raw: np.ndarray, total: int) -> np.ndarray:
    """Integer units summing exactly to `total`, by largest remainder.

    Rounding each cell on its own makes the generator's response to the volume
    knob a coarse staircase: every day sharing a shift pattern rounds at the
    same instant, so hundreds of cells tick together. Apportioning instead
    moves one unit at a time, which is what lets --target-rise land where it
    was asked to.
    """
    flat = raw.ravel()
    base = np.floor(flat)
    short = total - int(base.sum())
    if short > 0:
        order = np.argsort(-(flat - base), kind="stable")
        order = order[flat[order] > 0][:short]
        base[order] += 1.0
    return base.reshape(raw.shape)


@dataclass(frozen=True)
class Leak:
    meter: str
    start: date
    kw: float


def _cooling_kw(temp: np.ndarray) -> np.ndarray:
    out = np.zeros_like(temp)
    mid = (temp >= COOL_ONSET_C) & (temp < COOL_KINK_C)
    out[mid] = COOL_KW_PER_DEGC * (temp[mid] - COOL_ONSET_C)
    hot = temp >= COOL_KINK_C
    out[hot] = (COOL_KW_PER_DEGC * (COOL_KINK_C - COOL_ONSET_C)
                + COOL_KW_PER_DEGC_ABOVE_KINK * (temp[hot] - COOL_KINK_C))
    return out


def simulate(d: Drivers, noise: NoiseField, leak: Leak | None):
    """Return (ts, meter arrays, production rows, weather rows).

    Every meter comes from a physical driver. No meter is a function of the
    bridge, the tariff, or anything the analyser will later try to recover.
    """
    cal = d.calendar
    dates = list(cal["date"])
    n_days = len(dates)
    ts = pd.date_range(pd.Timestamp(dates[0]),
                       periods=n_days * INTERVALS_PER_DAY, freq="15min")

    occ = np.stack([OCCUPANCY[p] for p in cal["true_pattern"]])

    # Units per product per day, rounded before they drive anything, so the
    # emitted production.csv is exactly what produced the meter readings.
    factor = np.array([SHIFT_FACTOR[p] for p in cal["true_pattern"]])
    share = factor / factor.sum() if factor.sum() else np.zeros(n_days)
    weights = np.array([w for _, w in d.mix])
    units = _apportion(d.total_units * share[:, None] * weights[None, :],
                       int(round(d.total_units)))
    units_day = units.sum(axis=1)

    # Production is spread over running hours only.
    prod = np.where(occ >= 0.5, occ, 0.0)
    totals = prod.sum(axis=1, keepdims=True)
    prod = np.divide(prod, totals, out=np.zeros_like(prod), where=totals > 0)

    temp = d.temps[:, None] + INTRADAY_TEMP_AMPLITUDE_C * np.sin(
        2 * np.pi * (HOURS - 9.0) / 24.0)[None, :]

    intensities = np.array([PRODUCT_INTENSITY[p] for p, _ in d.mix])
    furnace_day = units @ intensities

    m = {}
    m["furnace"] = (furnace_day[:, None] * prod
                    + FURNACE_STANDBY_KW * STEP_HOURS * occ)
    for i in range(3):
        m[f"line_{i + 1}"] = (units_day[:, None] * LINE_INTENSITY[i] * prod
                              + LINE_STANDBY_KW[i] * STEP_HOURS * occ)
    m["hvac"] = ((HVAC_BASE_KW * (0.20 + 0.80 * occ)
                  + _cooling_kw(temp) * (0.30 + 0.70 * occ)
                  + HEAT_KW_PER_DEGC * np.maximum(0.0, HEAT_BALANCE_C - temp)
                  * (0.25 + 0.75 * occ)) * STEP_HOURS)
    m["chillers"] = (units_day[:, None] * CHILLER_KWH_PER_UNIT * prod
                     + CHILLER_KW_PER_DEGC * np.maximum(0.0, temp - 20.0)
                     * occ * STEP_HOURS)
    m["compressors"] = COMPRESSOR_KW * (0.25 + 0.75 * occ) * STEP_HOURS
    m["lighting"] = LIGHTING_KW * (0.15 + 0.85 * occ) * STEP_HOURS

    # The hidden waste: a leak runs flat out, day and night, from a known date.
    if leak is not None:
        after = np.array([dt >= leak.start for dt in dates])[:, None]
        m[leak.meter] = m[leak.meter] + after * leak.kw * STEP_HOURS

    main_true = (sum(m.values())
                 + UNMETERED_KW * (0.15 + 0.85 * occ) * STEP_HOURS)

    out = {}
    for name in SUBMETERS:
        out[name] = np.maximum(
            0.0, m[name] * (1.0 + SIGMA_SUBMETER * noise.rows(name, dates))
        ).ravel()
    # The main meter is measured independently, so sub-meters will not sum to
    # it -- that is edge case one, not a bug.
    out["main"] = np.maximum(
        0.0, main_true * (1.0 + SIGMA_MAIN * noise.rows("main", dates))
    ).ravel()

    production = pd.DataFrame([
        {"date": dates[i].isoformat(), "product": PRODUCTS[j],
         "units": int(units[i, j]),
         "shift_pattern": cal["shift_pattern"].iloc[i],
         "is_holiday": bool(cal["is_holiday"].iloc[i])}
        for i in range(n_days) for j in range(len(PRODUCTS))
    ])
    weather = pd.DataFrame({
        "date": [day.isoformat() for day in dates],
        "avg_temp_c": np.round(d.temps, 2),
        "cooling_degree_days": np.round(
            np.maximum(0.0, d.temps - DEGREE_DAY_BASE_C), 2),
        "heating_degree_days": np.round(
            np.maximum(0.0, DEGREE_DAY_BASE_C - d.temps), 2),
    })
    return ts, out, production, weather


def bill(ts: pd.DatetimeIndex, main_kwh: np.ndarray, tariff: Tariff) -> float:
    """The bill is the main supply priced interval by interval.

    Never a daily total against an average price -- that is the single biggest
    way to get the tariff term wrong.
    """
    return float(np.dot(main_kwh, tariff.price_array(ts)))


# --------------------------------------------------------------------------
# Shapley over the four causes
# --------------------------------------------------------------------------

def shapley(value, causes=CAUSES) -> dict:
    """Average each cause's marginal contribution over all orderings.

    Sequential attribution gives a different answer per ordering, and with four
    causes there are 24 of them. Shapley is order-free and still sums to
    value(all) - value(none).
    """
    n = len(causes)
    out = {c: 0.0 for c in causes}
    for c in causes:
        others = [x for x in causes if x != c]
        for r in range(n):
            w = factorial(r) * factorial(n - r - 1) / factorial(n)
            for subset in combinations(others, r):
                s = frozenset(subset)
                out[c] += w * (value(s | {c}) - value(s))
    return out


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------

PROFILES = {
    "primary": {
        "seed": 7,
        "target_rise": 0.15,
        "ref": ("2026-Q2", date(2026, 4, 1), date(2026, 6, 30)),
        "cur": ("2026-Q3", date(2026, 7, 1), date(2026, 9, 30)),
        "ref_mix": (("widget_a", 0.50), ("widget_b", 0.38), ("alloy_core", 0.12)),
        "cur_mix": (("widget_a", 0.475), ("widget_b", 0.37), ("alloy_core", 0.155)),
        "ref_units": 200_000.0,
        "ref_holidays": {date(2026, 5, 1): {"true_pattern": "down",
                                            "logged_pattern": "down",
                                            "logged_holiday": True}},
        "cur_holidays": {
            date(2026, 8, 15): {"true_pattern": "down",
                                "logged_pattern": "down",
                                "logged_holiday": True},
            # Trap: a real holiday logged as an ordinary two-shift day.
            date(2026, 9, 7): {"true_pattern": "skeleton",
                               "logged_pattern": "2-shift",
                               "logged_holiday": False},
        },
        "ref_tariff": Tariff(((date(2026, 4, 1), date(2026, 6, 30), 7.22, 4.03),)),
        "cur_tariff": Tariff((
            (date(2026, 7, 1), date(2026, 8, 15), 7.64, 4.28),
            # Edge case: the price changes in the middle of the quarter.
            (date(2026, 8, 16), date(2026, 9, 30), 8.02, 4.49),
        )),
        "warm_shift": 0.3,
        "excursion": (date(2026, 8, 5), date(2026, 8, 13), 3.5),
        "leak": Leak("compressors", date(2026, 8, 3), 15.0),
        "gaps": [("compressors", "2026-05-12T02:00", "2026-05-12T08:00"),
                 ("hvac", "2026-08-21T12:00", "2026-08-21T18:00"),
                 ("line_3", "2026-06-29T00:00", "2026-06-30T00:00")],
        "reset": ("line_2", "2026-07-18T03:00"),
    },
    "holdout": {
        "seed": 4201,
        "target_rise": 0.22,
        "ref": ("2026-Q2", date(2026, 4, 1), date(2026, 6, 30)),
        "cur": ("2026-Q3", date(2026, 7, 1), date(2026, 9, 30)),
        # Mix moves the other way: this set must not reward the same reflex.
        "ref_mix": (("widget_a", 0.33), ("widget_b", 0.40), ("alloy_core", 0.27)),
        "cur_mix": (("widget_a", 0.41), ("widget_b", 0.38), ("alloy_core", 0.21)),
        "ref_units": 236_000.0,
        "ref_holidays": {date(2026, 4, 14): {"true_pattern": "down",
                                             "logged_pattern": "down",
                                             "logged_holiday": True}},
        "cur_holidays": {
            date(2026, 9, 2): {"true_pattern": "skeleton",
                               "logged_pattern": "3-shift",
                               "logged_holiday": False},
        },
        "ref_tariff": Tariff(((date(2026, 4, 1), date(2026, 6, 30), 6.34, 4.70),)),
        "cur_tariff": Tariff((
            (date(2026, 7, 1), date(2026, 9, 2), 6.64, 4.96),
            (date(2026, 9, 3), date(2026, 9, 30), 6.89, 5.21),
        )),
        "warm_shift": 2.3,
        "excursion": (date(2026, 7, 20), date(2026, 7, 29), 5.2),
        # Different asset, different date, different size.
        "leak": Leak("chillers", date(2026, 7, 11), 21.0),
        "gaps": [("furnace", "2026-05-03T18:00", "2026-05-04T06:00"),
                 ("main", "2026-09-19T09:00", "2026-09-19T12:00")],
        "reset": ("lighting", "2026-08-27T23:15"),
    },
}


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def build(cfg: dict, target_rise: float, seed: int):
    ref_label, ref_start, ref_end = cfg["ref"]
    cur_label, cur_start, cur_end = cfg["cur"]

    ref_cal = build_calendar(ref_start, ref_end, cfg["ref_holidays"])
    cur_cal = build_calendar(cur_start, cur_end, cfg["cur_holidays"])
    noise = NoiseField(ref_start, cur_end, seed)

    ref = Drivers(ref_label, ref_cal, cfg["ref_units"], cfg["ref_mix"],
                  build_temps(ref_cal, seed, 0.0, None), cfg["ref_tariff"])

    def current(units: float) -> Drivers:
        return Drivers(cur_label, cur_cal, units, cfg["cur_mix"],
                       build_temps(cur_cal, seed + 1, cfg["warm_shift"],
                                   cfg["excursion"]),
                       cfg["cur_tariff"])

    ts_ref, m_ref, prod_ref, wx_ref = simulate(ref, noise, leak=None)
    actual_ref = bill(ts_ref, m_ref["main"], ref.tariff)

    def rise(units: float) -> float:
        d = current(units)
        ts, m, _, _ = simulate(d, noise, cfg["leak"])
        return bill(ts, m["main"], d.tariff) / actual_ref - 1.0

    # Volume is the free knob: solve it so the total rise lands on target.
    lo, hi = cfg["ref_units"] * 0.80, cfg["ref_units"] * 1.80
    for _ in range(48):
        mid = (lo + hi) / 2.0
        if rise(mid) < target_rise:
            lo = mid
        else:
            hi = mid
    cur = current((lo + hi) / 2.0)

    ts_cur, m_cur, prod_cur, wx_cur = simulate(cur, noise, cfg["leak"])
    actual_cur = bill(ts_cur, m_cur["main"], cur.tariff)

    @lru_cache(maxsize=None)
    def cost_of(active: frozenset) -> float:
        d = blend(ref, cur, active)
        ts, m, _, _ = simulate(d, noise, leak=None)
        return bill(ts, m["main"], d.tariff)

    # C(none) is the reference quarter re-simulated, so it must reproduce the
    # reference bill exactly. If this ever fails the bridge cannot add up.
    assert abs(cost_of(frozenset()) - actual_ref) < 1e-6, (
        f"C(none)={cost_of(frozenset()):.6f} != actual={actual_ref:.6f}")

    values = shapley(cost_of)
    waste = actual_cur - cost_of(frozenset(CAUSES))
    total = actual_cur - actual_ref
    assert abs(sum(values.values()) + waste - total) < 1e-6, "bridge does not add up"

    bridge = {c: {"cost": round(v, 2), "pp": round(100.0 * v / actual_ref, 4)}
              for c, v in values.items()}
    bridge["waste"] = {"cost": round(waste, 2),
                       "pp": round(100.0 * waste / actual_ref, 4)}

    frames, injected = _emit_frames(cfg, ts_ref, m_ref, prod_ref, wx_ref,
                                    ts_cur, m_cur, prod_cur, wx_cur)

    truth = {
        "reference_period": ref_label,
        "current_period": cur_label,
        "actual": {
            "reference_cost": round(actual_ref, 2),
            "current_cost": round(actual_cur, 2),
            "total_change": round(total, 2),
            "rise_pct": round(100.0 * total / actual_ref, 4),
        },
        "bridge_true": bridge,
        "counterfactuals": {
            ",".join(sorted(s)) or "none": round(cost_of(frozenset(s)), 2)
            for r in range(len(CAUSES) + 1) for s in combinations(CAUSES, r)
        },
        "drivers": {
            "reference_total_units": int(cfg["ref_units"]),
            "current_total_units": int(round(cur.total_units)),
            "reference_mix": dict(ref.mix),
            "current_mix": dict(cur.mix),
            "product_intensity_kwh_per_unit": PRODUCT_INTENSITY,
            "reference_mean_temp_c": round(float(ref.temps.mean()), 2),
            "current_mean_temp_c": round(float(cur.temps.mean()), 2),
        },
        "anomalies": {
            "leak": {
                "meter_id": cfg["leak"].meter,
                "start": cfg["leak"].start.isoformat(),
                "kw": cfg["leak"].kw,
                "kwh_total": round(cfg["leak"].kw * 24.0
                                   * (cur_end - cfg["leak"].start).days, 1),
            },
            "mislabelled_holiday": [
                {"date": d.isoformat(), "logged_as": v["logged_pattern"]}
                for d, v in cfg["cur_holidays"].items()
                if not v["logged_holiday"]],
            "temperature_excursion": {
                "from": cfg["excursion"][0].isoformat(),
                "to": cfg["excursion"][1].isoformat(),
                "bump_c": cfg["excursion"][2]},
            "mid_quarter_tariff_change": (
                cfg["cur_tariff"].windows[1][0].isoformat()
                if len(cfg["cur_tariff"].windows) > 1 else None),
            **injected,
        },
        "row_counts": {k: int(len(v)) for k, v in frames.items()},
        "known_limits": [
            "Cooling response is piecewise in temperature; a linear "
            "beta_cdd * CDD baseline cannot represent the kink above "
            f"{COOL_KINK_C} C, so some weather effect lands in the residual.",
            "Sub-meters are measured independently of the main meter and will "
            "not reconcile exactly.",
            "Single site, two quarters, three product families. No real "
            "company or personal data of any kind.",
        ],
    }
    return frames, injected, truth


def _emit_frames(cfg, ts_ref, m_ref, prod_ref, wx_ref,
                 ts_cur, m_cur, prod_cur, wx_cur):
    """Assemble the four tables, then corrupt the meter readings on purpose."""
    parts = []
    for ts, meters in ((ts_ref, m_ref), (ts_cur, m_cur)):
        stamps = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        for name in METERS:
            parts.append(pd.DataFrame({"meter_id": name, "ts": stamps,
                                       "kwh": np.round(meters[name], 4)}))
    meter = pd.concat(parts, ignore_index=True)

    injected = {"dropped_intervals": [], "meter_reset": None}

    # Missing intervals: whole blocks vanish, as they do when a logger drops out.
    for name, lo, hi in cfg["gaps"]:
        mask = ((meter["meter_id"] == name) & (meter["ts"] >= f"{lo}:00Z")
                & (meter["ts"] < f"{hi}:00Z"))
        injected["dropped_intervals"].append(
            {"meter_id": name, "from": lo, "to": hi, "rows": int(mask.sum())})
        meter = meter[~mask]

    # Meter reset: a cumulative counter rolls over and the delta goes negative.
    name, stamp = cfg["reset"]
    idx = meter.index[(meter["meter_id"] == name) & (meter["ts"] == f"{stamp}:00Z")]
    reset_value = -round(float(np.random.default_rng(1).uniform(3000, 6000)), 4)
    meter.loc[idx, "kwh"] = reset_value
    injected["meter_reset"] = {"meter_id": name, "ts": f"{stamp}:00Z",
                               "value": reset_value, "rows": int(len(idx))}

    meter = meter.sort_values(["ts", "meter_id"], kind="stable")
    meter = meter.reset_index(drop=True)

    return {
        "meter_15min": meter,
        "tariff": pd.concat([cfg["ref_tariff"].to_frame(),
                             cfg["cur_tariff"].to_frame()], ignore_index=True),
        "production": pd.concat([prod_ref, prod_cur], ignore_index=True),
        "weather": pd.concat([wx_ref, wx_cur], ignore_index=True),
    }, injected


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a synthetic factory dataset.")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="primary")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--target-rise", type=float, default=None,
                    help="fractional rise in the bill, e.g. 0.15")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = PROFILES[args.profile]
    seed = args.seed if args.seed is not None else cfg["seed"]
    target = args.target_rise if args.target_rise is not None else cfg["target_rise"]
    out = Path(args.out or f"data/{args.profile}")
    out.mkdir(parents=True, exist_ok=True)

    frames, injected, truth = build(cfg, target, seed)

    for name, df in frames.items():
        df.to_csv(out / f"{name}.csv", index=False)

    truth.update({
        "dataset_id": f"{args.profile}-s{seed}-r{int(round(target * 1000))}",
        "profile": args.profile,
        "seed": seed,
        "target_rise_pct": round(100.0 * target, 2),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": "rng/data/generator.py",
    })
    (out / "ground_truth.json").write_text(
        json.dumps(truth, indent=2), encoding="utf-8")

    (out / "dataset_meta.json").write_text(json.dumps({
        "dataset_id": truth["dataset_id"],
        "emission_factor_kg_co2e_per_kwh": 0.71,
        "currency": "INR",
        "peak_start_hour": cfg["cur_tariff"].peak_start_hour,
        "peak_end_hour": cfg["cur_tariff"].peak_end_hour,
        "peak_applies_on_weekends": False,
        "tariff_window_convention": (
            "[valid_from, valid_to) -- valid_to is exclusive"),
        "submeter_reconciliation_tolerance_pct": 6.0,
        "note": ("Values the analyser is allowed to read. "
                 "ground_truth.json is not."),
    }, indent=2), encoding="utf-8")

    b = truth["bridge_true"]
    print(f"{truth['dataset_id']}  ->  {out}")
    print(f"  {truth['reference_period']} {truth['actual']['reference_cost']:>12,.0f}"
          f"   {truth['current_period']} {truth['actual']['current_cost']:>12,.0f}"
          f"   rise {truth['actual']['rise_pct']:.2f}%")
    for cause in (*CAUSES, "waste"):
        print(f"  {cause:<10} {b[cause]['pp']:>7.2f} pp  {b[cause]['cost']:>12,.0f}")
    print(f"  {'total':<10} "
          f"{sum(b[c]['pp'] for c in (*CAUSES, 'waste')):>7.2f} pp")
    for k, v in truth["row_counts"].items():
        print(f"  rows  {k:<12} {v:>8,}")


if __name__ == "__main__":
    main()
