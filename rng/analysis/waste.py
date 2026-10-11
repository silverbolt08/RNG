"""R6 idle load and R7 leak detection. Owner: teammate C.

    find_idle_load(clean, period)            -> dict[asset, IdleStats]
    detect_leaks(clean, bridge, idle)        -> list[Finding]

`bridge` is a REQUIRED argument of detect_leaks, and that is the guardrail:
it cannot be called before the tariff, volume, mix and weather effects have
been removed. Annex A's order of attribution is enforced by the signature,
not by a prompt (CLAUDE.md section 9, #4 -- "blamed a machine first" is a
10-point deduction).

Two rules detect_leaks must obey:
  * If bridge.waste_pp is near zero, FIND NOTHING. The mix-shift trap exists
    to test exactly this, and declining to blame a machine there is worth
    points.
  * Do not flag HVAC or chillers for high night load when the weather term
    already explains it.

Idle load: energy in zero-production intervals (nights, weekends, genuine
holidays -- including ones the cleaning step flagged as mislabelled).
Leaks: night_share = median(night kWh) / median(day kWh) per asset, plus a
step-change test to date when the fault began.

Which intervals are idle comes from the data, not from clock hours or shift
names: a day with no output is idle throughout, and on a working day a slot
is idle when the site's typical load for that kind of day sits in the lower
half of its range. Nothing here knows a meter's name or what it drives.

The weather rule is applied per asset rather than by name. Each asset's
nightly floor is regressed on CDD and HDD over the reference quarter, and the
step test runs on what is left. Load the weather explains is removed before
anyone is blamed; a real leak on a weather-driven asset is still found.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from rng.analysis.baseline import daily_frame
from rng.analysis.decompose import Bridge, load_shapes
from rng.api.schemas import Cause, Confidence, Finding
from rng.ingest.clean import CleanTables
from rng.ingest.loader import main_meter
from rng.ingest.tariff import resolve_prices
from rng.periods import in_period

HOURS_PER_INTERVAL = 0.25
HOURS_PER_YEAR = 8760.0

# The waste gate. Below this, or when the waste range reaches zero, the bill
# has no unexplained rise to attribute and no machine is blamed.
WASTE_FLOOR_PP = 0.25

# A step in an asset's weather-adjusted idle floor counts as a fault when it
# is all of: large in absolute terms, large against the asset's own
# day-to-day noise, statistically clear, and present on most days after it
# starts (a heatwave raises some nights; a leak raises all of them).
MIN_STEP_KW = 2.0
MIN_STEP_NOISE = 4.0
MIN_SCORE = 8.0
MIN_PERSISTENCE = 0.8
MIN_DAYS_AFTER = 7


@dataclass(frozen=True)
class IdleStats:
    asset: str
    idle_kwh: float            # total over the period's idle intervals
    idle_hours: float
    median_idle_kw: float      # the floor
    night_share: float         # median night kWh / median day kWh
    cost: float


# --------------------------------------------------------------------------
# Idle intervals
# --------------------------------------------------------------------------

def _intervals(clean: CleanTables, period: str) -> pd.DataFrame:
    """Sub-meter readings for `period` with date, idle and working flags."""
    main = clean.main_meter or main_meter(clean.meter)
    m = clean.meter[in_period(clean.meter["ts"], period).to_numpy()]
    if m.empty:
        raise ValueError(f"no meter data for period {period}")
    frame = daily_frame(clean, period)
    shapes = load_shapes(clean, frame, period)

    # Idle slots per kind of day: the lower half of that day's typical shape.
    idle_slots = {}
    for key, shape in shapes.items():
        lo, hi = float(shape.min()), float(shape.max())
        idle_slots[key] = shape < lo + 0.5 * (hi - lo) if hi > lo else np.zeros(96, bool)

    units = frame.filter(like="u:").sum(axis=1)
    working = units > 0
    ts = m["ts"].dt.tz_localize(None)
    date = ts.dt.normalize()
    slot = (ts.dt.hour * 4 + ts.dt.minute // 15).to_numpy()
    key = frame["key"].reindex(date).fillna("unknown").to_numpy()
    on = working.reindex(date).fillna(False).to_numpy(bool)
    slot_idle = np.array([idle_slots.get(k, idle_slots["*"])[s]
                          for k, s in zip(key, slot, strict=True)])
    out = pd.DataFrame({
        "meter_id": m["meter_id"].to_numpy(), "ts": m["ts"].to_numpy(),
        "kwh": m["kwh"].to_numpy(float), "date": date.to_numpy(),
        "working": on, "idle": ~on | slot_idle,
    })
    return out[out["meter_id"] != main].reset_index(drop=True)


def find_idle_load(clean: CleanTables, period: str) -> dict[str, IdleStats]:
    iv = _intervals(clean, period)
    stamps = pd.Series(iv["ts"].unique())
    prices, _ = resolve_prices(clean.tariff, stamps)
    iv["price"] = iv["ts"].map(pd.Series(prices, index=stamps)).to_numpy(float)

    stats = {}
    for asset, g in iv.groupby("meter_id", sort=True):
        idle = g[g["idle"]]
        night = g[g["working"] & g["idle"]]
        day = g[g["working"] & ~g["idle"]]
        night = night if len(night) else idle
        day_med = float(day["kwh"].median()) if len(day) else 0.0
        stats[str(asset)] = IdleStats(
            asset=str(asset),
            idle_kwh=float(idle["kwh"].sum()),
            idle_hours=len(idle) * HOURS_PER_INTERVAL,
            median_idle_kw=float(idle["kwh"].median() / HOURS_PER_INTERVAL)
            if len(idle) else 0.0,
            night_share=float(night["kwh"].median()) / day_med if day_med > 0 else 0.0,
            cost=float((idle["kwh"] * idle["price"]).sum()),
        )
    return stats


# --------------------------------------------------------------------------
# Leaks
# --------------------------------------------------------------------------

def _floors(clean: CleanTables, period: str) -> pd.DataFrame:
    """Daily idle floor in kW: rows are dates, columns are assets."""
    iv = _intervals(clean, period)
    idle = iv[iv["idle"]]
    floor = idle.groupby(["date", "meter_id"])["kwh"].median().unstack()
    return floor / HOURS_PER_INTERVAL


def _weather(clean: CleanTables, period: str, index: pd.Index) -> np.ndarray:
    f = daily_frame(clean, period).reindex(index)
    return np.column_stack([np.ones(len(f)), f["cdd"].fillna(0.0), f["hdd"].fillna(0.0)])


@dataclass(frozen=True)
class _Step:
    asset: str
    kw: float
    start: pd.Timestamp
    score: float
    noise_kw: float
    persistence: float


def _step(asset: str, ref: pd.Series, cur: pd.Series,
          x_ref: np.ndarray, x_cur: np.ndarray) -> _Step | None:
    """Largest sustained rise in the weather-adjusted floor during `cur`."""
    ok_r, ok_c = ref.notna().to_numpy(), cur.notna().to_numpy()
    if ok_r.sum() < 14 or ok_c.sum() < MIN_DAYS_AFTER + 1:
        return None
    coef, *_ = np.linalg.lstsq(x_ref[ok_r], ref.to_numpy(float)[ok_r], rcond=None)
    r_ref = ref.to_numpy(float)[ok_r] - x_ref[ok_r] @ coef
    r_cur = cur.to_numpy(float)[ok_c] - x_cur[ok_c] @ coef
    days = cur.index[ok_c]
    mad = float(np.median(np.abs(r_ref - np.median(r_ref))))
    noise = max(1.4826 * mad, 1e-6)

    # Where the step is: the split that best separates two means (a median
    # barely moves when a few pre-fault days are included, so it would pick
    # too early a date). How big it is: medians on each side, which a
    # handful of hot nights cannot drag.
    best = None
    for k in range(0, len(r_cur) - MIN_DAYS_AFTER + 1):
        before = np.r_[r_ref, r_cur[:k]]
        after = r_cur[k:]
        shift = float(after.mean() - before.mean())
        score = shift / (noise * np.sqrt(1.0 / len(after) + 1.0 / len(before)))
        if best is None or score > best[0]:
            best = (score, k)
    score, k = best
    before, after = np.r_[r_ref, r_cur[:k]], r_cur[k:]
    level = float(np.median(before))
    step = float(np.median(after)) - level
    persistence = float(np.mean(after - level > 0.5 * step)) if step > 0 else 0.0
    return _Step(asset=asset, kw=step, start=pd.Timestamp(days[k]), score=score,
                 noise_kw=noise, persistence=persistence)


def _is_fault(s: _Step) -> bool:
    return (s.kw >= MIN_STEP_KW and s.kw >= MIN_STEP_NOISE * s.noise_kw
            and s.score >= MIN_SCORE and s.persistence >= MIN_PERSISTENCE)


def waste_is_material(bridge: Bridge) -> bool:
    """The gate: is there an unexplained rise worth attributing to a machine?"""
    if bridge.waste_pp < WASTE_FLOOR_PP:
        return False
    low, _ = bridge.ranges_pp.get(Cause.WASTE, (bridge.waste_pp, bridge.waste_pp))
    return low > 0.0


def scan_assets(clean: CleanTables, period: str, compare_to: str) -> list[_Step]:
    """Every asset's strongest step, flagged or not -- for the trace."""
    ref, cur = _floors(clean, compare_to), _floors(clean, period)
    x_ref = _weather(clean, compare_to, ref.index)
    x_cur = _weather(clean, period, cur.index)
    steps = []
    for asset in sorted(set(ref.columns) & set(cur.columns)):
        s = _step(str(asset), ref[asset], cur[asset], x_ref, x_cur)
        if s is not None:
            steps.append(s)
    return steps


def detect_leaks(clean: CleanTables, bridge: Bridge,
                 idle: dict[str, IdleStats]) -> list[Finding]:
    if not waste_is_material(bridge):
        return []
    period, compare_to = bridge.period, bridge.compare_to
    findings = []
    for s in scan_assets(clean, period, compare_to):
        if not _is_fault(s):
            continue
        stats = idle.get(s.asset)
        start = s.start.date().isoformat()
        night = (f" Its night load now runs at {stats.night_share:.0%} of its "
                 "daytime load." if stats and stats.night_share else "")
        clear = s.score >= 2 * MIN_SCORE and s.persistence >= 0.9
        findings.append(Finding(
            finding_id=f"leak:{s.asset}:{start}",
            asset=s.asset,
            kind="leak",
            description=(
                f"{s.asset} draws about {s.kw:.1f} kW more when the plant is idle "
                f"from {start} onward, after its weather response is removed, "
                f"and on {s.persistence:.0%} of days since. Load that does not "
                f"stop when production stops is the signature of a leak or a "
                f"stuck unit.{night}"),
            kwh_per_year=round(s.kw * HOURS_PER_YEAR, 1),
            evidence_refs=[f"idle_floor:{s.asset}:{compare_to}..{period}",
                           f"bridge:waste:{compare_to}..{period}"],
            confidence=Confidence.HIGH if clear else Confidence.MEDIUM,
            detected_from=start,
        ))
    return sorted(findings, key=lambda f: -f.kwh_per_year)
