"""Price per 15-minute interval. Owner: teammate B.

Annex B's tariff table names the PERIOD (peak | off_peak | flat) but never says
which HOURS are peak, so joining on valid_from / valid_to alone returns two
rows per timestamp. This function is the single place that ambiguity is
resolved, for our data and for the judges'.

Cases it must handle:
  1. peak_start_hour / peak_end_hour columns present -> use them.
     Absent -> the defaults in rng.config.assumptions(), and say so in the
     returned convention so it lands in the response's assumptions[].
  2. period == "flat": one price for every hour. Our generator never emits
     it; a judge's file may.
  3. valid_to written half-open (next window's start) OR inclusive
     (23:59:59). Join with `valid_from <= ts < valid_to`, treating an
     inclusive end as covering its whole final day.
  4. A timestamp no window covers: raise. Never price it at zero.

The tariff is the largest bridge term. It is priced interval by interval and
never as a daily total against an average price (CLAUDE.md section 9, #2).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def resolve_prices(tariff: pd.DataFrame, ts: pd.Series) -> tuple[np.ndarray, str]:
    """Return (price per interval aligned to `ts`, the convention applied)."""
    raise NotImplementedError
