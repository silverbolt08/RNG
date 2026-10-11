"""R3 bill decomposition -- Shapley, with waste as the residual. Owner: teammate C.

C(S) = the bill if the causes in S sit at current-period levels and the rest
stay at the reference period. Four causes -> 16 memoised evaluations.

    tariff, volume, mix, weather = Shapley values over C  (sum: C(all) - C(none))
    waste                        = actual_cost - C(all)   (the residual)
    sum of all five              = actual_current - actual_reference, exactly

Three rules (CLAUDE.md section 9):
  #2  Price INTERVAL BY INTERVAL. Spread predicted daily kWh across the
      15-minute grid using the reference load shape, then multiply by
      rng.ingest.tariff.resolve_prices(). Never daily totals x average price:
      the tariff is the largest term and that is how it goes wrong.
  #3  Waste is the RESIDUAL. Never estimate it separately -- the five numbers
      stop genuinely adding up and additivity becomes a fudge.
  --  actual_cost comes from real meter readings at the real tariff, never
      from the model. Additivity is then structural; assert_adds_up() is a
      regression guard, not a reconciliation mechanism.

What each cause switches, for a day-by-day model:
  volume  -> the calendar and each day's total units (how much, and on which
             days, the plant ran). C(none) is then the reference quarter
             itself, which is what anchors the bridge.
  mix     -> each family's share of those units.
  weather -> the CDD / HDD series, laid on the active calendar by position.
  tariff  -> the price of every interval. The other quarter's prices are laid
             on the active calendar shifted by whole weeks, so a weekday stays
             a weekday and the peak window still lands on weekdays.

C(S) = actual_reference + M(S) - M(none), where M is the model-priced bill.
The model's error on the reference quarter is therefore held fixed across all
16 counterfactuals instead of leaking into one cause.

Ranges: refit the baseline on resamples, recompute the split, report the
10th / 90th percentiles. Resampling is by whole weeks, because daily energy
is autocorrelated and day-by-day resampling would make the ranges too narrow.
No range is invented.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from itertools import combinations, permutations
from math import factorial

import numpy as np
import pandas as pd

from rng.analysis.baseline import (
    BaselineModel,
    daily_frame,
    fit_frame,
    products,
    refine_frames,
)
from rng.api.schemas import BridgeItem, Cause
from rng.ingest.clean import CleanTables
from rng.ingest.loader import main_meter
from rng.ingest.tariff import resolve_prices
from rng.periods import in_period

MODELLED_CAUSES = (Cause.TARIFF, Cause.VOLUME, Cause.PRODUCT_MIX, Cause.WEATHER)

SLOTS = 96                               # 15-minute intervals per day
_SLOT = pd.Timedelta(minutes=15)
_WEEK = pd.Timedelta(days=7)
RESAMPLES = 50
PERCENTILES = (10.0, 90.0)
SEED = 20261011                          # ranges are reproducible run to run


@dataclass(frozen=True)
class Bridge:
    period: str
    compare_to: str
    reference_cost: float
    current_cost: float
    effects: dict[Cause, float]                    # currency; all five causes
    ranges_pp: dict[Cause, tuple[float, float]] = field(default_factory=dict)
    r2: float = 0.0
    details: dict = field(default_factory=dict)    # for the trace and registry

    @property
    def total_change(self) -> float:
        return self.current_cost - self.reference_cost

    @property
    def total_pct(self) -> float:
        return 100.0 * self.total_change / self.reference_cost

    def pct_points(self, cause: Cause) -> float:
        return 100.0 * self.effects[cause] / self.reference_cost

    @property
    def waste_pp(self) -> float:
        return self.pct_points(Cause.WASTE)

    def assert_adds_up(self, tol: float = 1e-6) -> None:
        gap = sum(self.effects.values()) - self.total_change
        assert abs(gap) <= tol * max(1.0, abs(self.total_change)), (
            f"bridge is off by {gap:.6f}")

    def to_items(self) -> list[BridgeItem]:
        """The wire format for AnalyzeResponse.bridge."""
        items = []
        for cause in Cause:
            pp = self.pct_points(cause)
            low, high = self.ranges_pp.get(cause, (pp, pp))
            items.append(BridgeItem(cause=cause, pct_points=round(pp, 2),
                                    range=(round(min(low, pp), 2),
                                           round(max(high, pp), 2)),
                                    amount=round(self.effects[cause], 2)))
        return items


# --------------------------------------------------------------------------
# Attribution over a set function
# --------------------------------------------------------------------------

def shapley(value: Callable[[frozenset], float],
            causes: Iterable = MODELLED_CAUSES) -> dict:
    """Each cause's marginal contribution averaged over every ordering.

    Order-free, and sums to value(all) - value(none).
    """
    causes = tuple(causes)
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


def sequential(value: Callable[[frozenset], float], order: Iterable) -> dict:
    """Laspeyres-style attribution: switch causes on one at a time in `order`.

    Also sums to value(all) - value(none), but each cause's share depends on
    where it sits in the order. Kept for the Shapley-versus-sequential
    comparison, never used in an answer.
    """
    out, active = {}, frozenset()
    for c in order:
        out[c] = value(active | {c}) - value(active)
        active = active | {c}
    return out


def order_spread(value: Callable[[frozenset], float],
                 causes: Iterable = MODELLED_CAUSES) -> dict:
    """(min, max) of each cause's sequential share across all orderings.

    The method-neutral case for Shapley: a sequential split gives up to 24
    different answers for four causes; Shapley gives one.
    """
    causes = tuple(causes)
    seen: dict = {c: [] for c in causes}
    for order in permutations(causes):
        for c, v in sequential(value, order).items():
            seen[c].append(v)
    return {c: (min(v), max(v)) for c, v in seen.items()}


def subset_label(s: Iterable) -> str:
    return ",".join(sorted(str(c) for c in s)) or "none"


# --------------------------------------------------------------------------
# Everything that does not depend on the fitted model, computed once
# --------------------------------------------------------------------------

def _grid(dates: pd.DatetimeIndex) -> pd.Series:
    """Every 15-minute timestamp (UTC) of these days, day-major."""
    start = dates.to_numpy(dtype="datetime64[ns]")[:, None]
    steps = (np.arange(SLOTS) * np.timedelta64(15, "m")).astype("timedelta64[ns]")
    return pd.Series((start + steps[None, :]).ravel()).dt.tz_localize("UTC")


def _onto(stamps: pd.Series, src: pd.DatetimeIndex) -> pd.Series:
    """Shift timestamps by whole weeks until they fall inside `src`'s days."""
    lo = pd.Timestamp(src.min(), tz="UTC")
    hi = pd.Timestamp(src.max(), tz="UTC") + pd.Timedelta(days=1)
    first = pd.Timestamp(stamps.min()).normalize()
    weeks = round((lo - first) / _WEEK)
    out = stamps + weeks * _WEEK
    for _ in range(60):
        early, late = out < lo, out >= hi
        if not (early.any() or late.any()):
            return out
        out = out + early * _WEEK - late * _WEEK
    raise ValueError("could not lay one period's tariff on the other's calendar")


def _resample(values: np.ndarray, n: int) -> np.ndarray:
    """A series laid on a calendar of a different length, by position."""
    if len(values) == n:
        return values
    src = np.linspace(0.0, 1.0, len(values))
    return np.interp(np.linspace(0.0, 1.0, n), src, values)


def load_shapes(clean: CleanTables, frame: pd.DataFrame, period: str) -> dict:
    """Mean share of each day's main-meter energy per slot, by day type."""
    main = clean.main_meter or main_meter(clean.meter)
    m = clean.meter[(clean.meter["meter_id"] == main)
                    & in_period(clean.meter["ts"], period).to_numpy()]
    ts = m["ts"].dt.tz_localize(None)
    grid = pd.DataFrame({"date": ts.dt.normalize().to_numpy(),
                         "slot": (ts.dt.hour * 4 + ts.dt.minute // 15).to_numpy(),
                         "kwh": m["kwh"].to_numpy()})
    mat = grid.pivot_table(index="date", columns="slot", values="kwh",
                           aggfunc="sum").reindex(columns=range(SLOTS)).fillna(0.0)
    total = mat.sum(axis=1)
    mat = mat[total > 0].div(total[total > 0], axis=0)
    keys = frame["key"].reindex(mat.index).fillna("unknown")
    shapes = {k: g.mean().to_numpy() for k, g in mat.groupby(keys)}
    shapes["*"] = mat.mean().to_numpy() if len(mat) else np.full(SLOTS, 1.0 / SLOTS)
    return shapes


@dataclass
class _Setup:
    labels: dict[str, str]                       # "ref"/"cur" -> period label
    frames: dict[str, pd.DataFrame]
    names: list[str]                             # product families
    shares: dict[str, np.ndarray]                # period -> share per family
    eff_price: dict[tuple[str, str], np.ndarray]  # (calendar, tariff) -> per day
    weather: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]]
    actual: dict[str, float]
    convention: str

    def total_units(self, side: str) -> np.ndarray:
        f = self.frames[side]
        return sum((f[f"u:{p}"].to_numpy(float) for p in self.names
                    if f"u:{p}" in f), np.zeros(len(f)))


def _actual_cost(clean: CleanTables, period: str) -> tuple[float, str]:
    main = clean.main_meter or main_meter(clean.meter)
    m = clean.meter[(clean.meter["meter_id"] == main)
                    & in_period(clean.meter["ts"], period).to_numpy()]
    prices, convention = resolve_prices(clean.tariff, m["ts"])
    return float(np.dot(m["kwh"].to_numpy(float), prices)), convention


def _setup(clean: CleanTables, period: str, compare_to: str) -> _Setup:
    labels = {"ref": compare_to, "cur": period}
    frames = {s: daily_frame(clean, p) for s, p in labels.items()}
    names = products(*frames.values())
    shapes = load_shapes(clean, frames["ref"], compare_to)

    shares = {}
    for s, f in frames.items():
        tot = np.array([f[f"u:{p}"].sum() if f"u:{p}" in f else 0.0 for p in names])
        shares[s] = tot / tot.sum() if tot.sum() else np.zeros(len(names))

    eff, weather = {}, {}
    for cal, f in frames.items():
        day_shape = np.vstack([shapes.get(k, shapes["*"]) for k in f["key"]])
        stamps = _grid(f.index)
        for tar in ("ref", "cur"):
            ts = stamps if tar == cal else _onto(stamps, frames[tar].index)
            prices, _ = resolve_prices(clean.tariff, ts)
            eff[cal, tar] = (day_shape * prices.reshape(len(f), SLOTS)).sum(axis=1)
            src = frames[tar]
            weather[cal, tar] = (_resample(src["cdd"].to_numpy(float), len(f)),
                                 _resample(src["hdd"].to_numpy(float), len(f)))

    actual, convention = {}, ""
    for s, p in labels.items():
        actual[s], convention = _actual_cost(clean, p)
    return _Setup(labels, frames, names, shares, eff, weather, actual, convention)


# --------------------------------------------------------------------------
# The counterfactual bill and the split
# --------------------------------------------------------------------------

def _value_fn(setup: _Setup, model: BaselineModel) -> Callable[[frozenset], float]:
    """C(S), memoised over the 16 subsets."""
    memo: dict[frozenset, float] = {}

    def model_bill(s: frozenset) -> float:
        cal = "cur" if Cause.VOLUME in s else "ref"
        mix = "cur" if Cause.PRODUCT_MIX in s else "ref"
        wx = "cur" if Cause.WEATHER in s else "ref"
        tar = "cur" if Cause.TARIFF in s else "ref"
        total = setup.total_units(cal)
        units = {p: total * setup.shares[mix][i] for i, p in enumerate(setup.names)}
        cdd, hdd = setup.weather[cal, wx]
        kwh_day = np.clip(model.predict(units, cdd, hdd), 0.0, None)
        return float(np.dot(kwh_day, setup.eff_price[cal, tar]))

    anchor = setup.actual["ref"] - model_bill(frozenset())

    def value(s: frozenset) -> float:
        s = frozenset(s)
        if s not in memo:
            memo[s] = model_bill(s)
        return anchor + memo[s]
    return value


def _split(setup: _Setup, model: BaselineModel) -> tuple[dict, Callable]:
    value = _value_fn(setup, model)
    effects = shapley(value)
    effects[Cause.WASTE] = setup.actual["cur"] - value(frozenset(MODELLED_CAUSES))
    return effects, value


def _weekly_resample(frame: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    week = frame.index.to_period("W")
    blocks = [g for _, g in frame.groupby(week)]
    pick = rng.integers(0, len(blocks), len(blocks))
    return pd.concat([blocks[i] for i in pick])


def _ranges(setup: _Setup, model: BaselineModel, n: int) -> dict:
    """10th / 90th percentile of each cause, in pp, over weekly resamples."""
    rng = np.random.default_rng(SEED)
    ref_cost = setup.actual["ref"]
    draws: dict[Cause, list[float]] = {c: [] for c in Cause}
    for _ in range(n):
        ref = _weekly_resample(setup.frames["ref"], rng)
        cur = _weekly_resample(setup.frames["cur"], rng)
        try:
            m = fit_frame(ref, model.fitted_on, setup.names)
            m = refine_frames(m, ref, cur, setup.labels["cur"])
        except (ValueError, np.linalg.LinAlgError):
            continue
        effects, _ = _split(setup, m)
        for c, v in effects.items():
            draws[c].append(100.0 * v / ref_cost)
    return {c: (float(np.percentile(v, PERCENTILES[0])),
                float(np.percentile(v, PERCENTILES[1])))
            for c, v in draws.items() if v}


def _bridge(setup: _Setup, model: BaselineModel, resamples: int) -> Bridge:
    refined = refine_frames(model, setup.frames["ref"], setup.frames["cur"],
                            setup.labels["cur"])
    effects, value = _split(setup, refined)
    counterfactuals = {}
    for r in range(len(MODELLED_CAUSES) + 1):
        for s in combinations(MODELLED_CAUSES, r):
            counterfactuals[subset_label(s)] = round(value(frozenset(s)), 2)
    intensity = {
        side: float(sum(refined.beta_units.get(p, 0.0) * setup.shares[side][i]
                        for i, p in enumerate(setup.names)))
        for side in ("ref", "cur")}
    bridge = Bridge(
        period=setup.labels["cur"], compare_to=setup.labels["ref"],
        reference_cost=setup.actual["ref"], current_cost=setup.actual["cur"],
        effects=effects,
        ranges_pp=_ranges(setup, model, resamples) if resamples else {},
        r2=refined.r2,
        details={
            "method": "shapley over 16 counterfactuals; waste is the residual",
            "counterfactuals": counterfactuals,
            "kwh_per_unit": {setup.labels[s]: round(v, 4) for s, v in intensity.items()},
            "beta_units": {p: round(b, 4) for p, b in refined.beta_units.items()},
            "beta_cdd": round(refined.beta_cdd, 3),
            "beta_hdd": round(refined.beta_hdd, 3),
            "r2": round(refined.r2, 4),
            "baseline_fitted_on": refined.fitted_on,
            "intensity_from": list(refined.intensity_from),
            "resamples": resamples,
            "range_percentiles": list(PERCENTILES),
            "tariff_convention": setup.convention,
        })
    bridge.assert_adds_up()
    return bridge


def decompose(clean: CleanTables, period: str, compare_to: str,
              model: BaselineModel) -> Bridge:
    """Split the bill change between `compare_to` and `period` into five causes."""
    if model.fitted_on != compare_to:
        raise ValueError(f"baseline was fitted on {model.fitted_on}, not on the "
                         f"reference period {compare_to}")
    return _bridge(_setup(clean, period, compare_to), model, RESAMPLES)


def attribution_spread(clean: CleanTables, period: str, compare_to: str,
                       model: BaselineModel) -> dict[Cause, dict[str, float]]:
    """Shapley next to the min / max sequential split across all 24 orderings.

    In pp of the reference bill. This is the comparison that justifies Shapley
    without any answer key: sequential attribution moves with the order.
    """
    setup = _setup(clean, period, compare_to)
    refined = refine_frames(model, setup.frames["ref"], setup.frames["cur"], period)
    effects, value = _split(setup, refined)
    spread = order_spread(value)
    ref = setup.actual["ref"]
    return {c: {"shapley": 100.0 * effects[c] / ref,
                "sequential_min": 100.0 * spread[c][0] / ref,
                "sequential_max": 100.0 * spread[c][1] / ref}
            for c in MODELLED_CAUSES}
