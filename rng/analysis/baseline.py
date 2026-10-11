"""R2 baseline. Owner: teammate C.

    kwh_day ~ b0 + sum_p(beta_p * units_p) + beta_cdd * CDD + beta_hdd * HDD

One coefficient per product family, so beta_p reads as "kWh per unit of
product p" -- explainable in the Q&A, and invertible for counterfactuals.

FIT ON THE REFERENCE PERIOD ONLY (CLAUDE.md section 9, #1). Fit on both
quarters and the leak becomes "normal": waste collapses to zero and the
controllable part of the bill disappears from the answer.

One refinement, and why. When the product mix is the same every day of a
quarter, that quarter alone cannot tell the families apart: the three unit
columns move in lockstep and only kWh per unit *at that mix* is identified.
The mix effect is then unmeasurable and lands in waste -- the mix-shift trap,
failed. `refine_intensities` therefore re-estimates the per-unit coefficients
from day-to-day variation within BOTH quarters, giving each quarter its own
level term. The level, where a constant leak lives, is still never learned
from the current quarter, and neither is the weather response.

Fits from the cleaned tables alone. This package may never import rng.data
or read the generator's answer key -- tests/test_no_generator_import.py
enforces it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from rng.ingest.clean import CleanTables
from rng.ingest.loader import main_meter
from rng.periods import in_period

# Ridge weight pulling each beta_p toward the families' common rate, as a
# share of the per-family unit variance. Tiny: it only decides directions the
# data says nothing about (lockstep families), never ones it does.
SHRINK = 1e-4


@dataclass(frozen=True)
class BaselineModel:
    fitted_on: str                 # the period label it was fitted on
    intercept: float               # kWh per day
    beta_units: dict[str, float]   # product -> kWh per unit
    beta_cdd: float
    beta_hdd: float
    r2: float
    n_days: int
    intensity_from: tuple[str, ...] = ()   # periods beta_units was refined on

    def predict(self, units: dict[str, np.ndarray], cdd: np.ndarray,
                hdd: np.ndarray) -> np.ndarray:
        """Expected main-meter kWh per day."""
        cdd = np.asarray(cdd, dtype=float)
        out = self.intercept + self.beta_cdd * cdd + self.beta_hdd * np.asarray(hdd, float)
        common = float(np.mean(list(self.beta_units.values()))) if self.beta_units else 0.0
        for product, u in units.items():
            out = out + self.beta_units.get(product, common) * np.asarray(u, float)
        return out


# --------------------------------------------------------------------------
# Daily table: one row per date, from the cleaned data only
# --------------------------------------------------------------------------

def day_key(shift_pattern, is_holiday, suspected) -> str:
    """Which load shape a day follows. Holidays share one shape however they
    were logged, including days the cleaning step flagged as mislabelled."""
    if bool(is_holiday) or bool(suspected):
        return "holiday"
    text = "" if pd.isna(shift_pattern) else str(shift_pattern).strip().lower()
    return text or "unknown"


def daily_frame(clean: CleanTables, period: str) -> pd.DataFrame:
    """date, kwh (main meter), units per product, cdd, hdd, key.

    Indexed by tz-naive midnight dates. Days without production rows have
    zero units; weather gaps are interpolated.
    """
    main = clean.main_meter or main_meter(clean.meter)
    m = clean.meter[(clean.meter["meter_id"] == main)
                    & in_period(clean.meter["ts"], period).to_numpy()]
    if m.empty:
        raise ValueError(f"no main-meter data for period {period}")
    dates = m["ts"].dt.tz_localize(None).dt.normalize()
    kwh = m.groupby(dates.to_numpy())["kwh"].sum()
    kwh.index = pd.DatetimeIndex(kwh.index)
    out = pd.DataFrame({"kwh": kwh})

    p = clean.production[in_period(clean.production["date"], period).to_numpy()]
    units = (p.pivot_table(index="date", columns="product", values="units",
                           aggfunc="sum").astype(float)
             if not p.empty else pd.DataFrame(index=out.index))
    out = out.join(units.add_prefix("u:"), how="left")
    ucols = [c for c in out.columns if c.startswith("u:")]
    out[ucols] = out[ucols].fillna(0.0)

    w = clean.weather.set_index("date")[["cooling_degree_days", "heating_degree_days"]]
    w = w.reindex(out.index.union(w.index)).sort_index().interpolate(
        limit_direction="both").reindex(out.index)
    out["cdd"] = w["cooling_degree_days"].fillna(0.0).to_numpy()
    out["hdd"] = w["heating_degree_days"].fillna(0.0).to_numpy()

    if p.empty:
        out["key"] = "unknown"
    else:
        p = p.assign(
            hol=p["is_holiday"].fillna(False).astype(bool),
            sus=(p["suspected_holiday"].fillna(False).astype(bool)
                 if "suspected_holiday" in p.columns else False))
        flags = p.groupby("date").agg(pattern=("shift_pattern", "first"),
                                      hol=("hol", "any"), sus=("sus", "any"))
        keys = {d: day_key(r.pattern, r.hol, r.sus) for d, r in flags.iterrows()}
        out["key"] = [keys.get(d, "unknown") for d in out.index]
    return out


def products(*frames: pd.DataFrame) -> list[str]:
    found = {c[2:] for f in frames for c in f.columns if c.startswith("u:")}
    return sorted(found)


# --------------------------------------------------------------------------
# Fitting
# --------------------------------------------------------------------------

def _units(frame: pd.DataFrame, names: list[str]) -> np.ndarray:
    return np.column_stack([frame[f"u:{p}"].to_numpy(float) if f"u:{p}" in frame
                            else np.zeros(len(frame)) for p in names]) \
        if names else np.zeros((len(frame), 0))


def _shrink_rows(u: np.ndarray, n_before: int, n_after: int) -> np.ndarray:
    """Penalty rows sqrt(lam) * (beta_p - mean beta) = 0 for each family."""
    k = u.shape[1]
    if k < 2:
        return np.zeros((0, n_before + k + n_after))
    lam = SHRINK * float((u ** 2).sum()) / k
    centre = np.eye(k) - np.full((k, k), 1.0 / k)
    return np.hstack([np.zeros((k, n_before)), np.sqrt(lam) * centre,
                      np.zeros((k, n_after))])


def fit_frame(frame: pd.DataFrame, period: str,
              names: list[str] | None = None) -> BaselineModel:
    """OLS on one period's daily rows (with the lockstep-family penalty)."""
    names = products(frame) if names is None else names
    if len(frame) < len(names) + 4:
        raise ValueError(f"{period}: {len(frame)} days is too few to fit a baseline")
    u = _units(frame, names)
    X = np.column_stack([np.ones(len(frame)), u, frame["cdd"], frame["hdd"]])
    y = frame["kwh"].to_numpy(float)
    pen = _shrink_rows(u, 1, 2)
    coef, *_ = np.linalg.lstsq(np.vstack([X, pen]), np.r_[y, np.zeros(len(pen))],
                               rcond=None)
    fitted = X @ coef
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float(((y - fitted) ** 2).sum()) / ss_tot if ss_tot else 0.0
    k = len(names)
    return BaselineModel(
        fitted_on=period, intercept=float(coef[0]),
        beta_units={p: float(b) for p, b in zip(names, coef[1:1 + k], strict=True)},
        beta_cdd=float(coef[1 + k]), beta_hdd=float(coef[2 + k]),
        r2=r2, n_days=len(frame))


def fit_baseline(clean: CleanTables, period: str) -> BaselineModel:
    """Fit on `period` (the reference quarter) and nothing else."""
    return fit_frame(daily_frame(clean, period), period)


def refine_frames(model: BaselineModel, ref: pd.DataFrame, cur: pd.DataFrame,
                  current: str) -> BaselineModel:
    """Re-estimate kWh per unit from within-period variation in both quarters.

    The weather response is held at the reference fit. Each quarter gets its
    own level term, so extra load that does not move with production -- a
    leak, a stuck schedule -- cannot leak into the per-unit coefficients. The
    reference level becomes the model's intercept; the current one is
    discarded, because the difference between them is exactly what the
    residual has to keep.
    """
    names = products(ref, cur)
    if not names:
        return model
    rows, ys, levels = [], [], []
    for i, f in enumerate((ref, cur)):
        y = (f["kwh"].to_numpy(float) - model.beta_cdd * f["cdd"].to_numpy(float)
             - model.beta_hdd * f["hdd"].to_numpy(float))
        lv = np.zeros((len(f), 2))
        lv[:, i] = 1.0
        levels.append(lv)
        rows.append(_units(f, names))
        ys.append(y)
    u = np.vstack(rows)
    X = np.hstack([np.vstack(levels), u])
    pen = _shrink_rows(u, 2, 0)
    coef, *_ = np.linalg.lstsq(np.vstack([X, pen]),
                               np.r_[np.concatenate(ys), np.zeros(len(pen))],
                               rcond=None)
    refined = replace(
        model, intercept=float(coef[0]),
        beta_units={p: float(b) for p, b in zip(names, coef[2:], strict=True)},
        intensity_from=(model.fitted_on, current))
    # R2 is reported on the reference quarter, where the model is a fit.
    y = ref["kwh"].to_numpy(float)
    pred = refined.predict({p: ref[f"u:{p}"].to_numpy(float) if f"u:{p}" in ref
                            else np.zeros(len(ref)) for p in names},
                           ref["cdd"], ref["hdd"])
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float(((y - pred) ** 2).sum()) / ss_tot if ss_tot else 0.0
    return replace(refined, r2=r2)


def refine_intensities(clean: CleanTables, model: BaselineModel,
                       current: str) -> BaselineModel:
    """`refine_frames` over the cleaned tables; see there."""
    return refine_frames(model, daily_frame(clean, model.fitted_on),
                         daily_frame(clean, current), current)
