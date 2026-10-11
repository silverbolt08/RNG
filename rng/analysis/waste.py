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
"""

from __future__ import annotations

from dataclasses import dataclass

from rng.analysis.decompose import Bridge
from rng.api.schemas import Finding
from rng.ingest.clean import CleanTables


@dataclass(frozen=True)
class IdleStats:
    asset: str
    idle_kwh: float            # total over the period's idle intervals
    idle_hours: float
    median_idle_kw: float      # the floor
    night_share: float         # median night kWh / median day kWh
    cost: float


def find_idle_load(clean: CleanTables, period: str) -> dict[str, IdleStats]:
    raise NotImplementedError


def detect_leaks(clean: CleanTables, bridge: Bridge,
                 idle: dict[str, IdleStats]) -> list[Finding]:
    raise NotImplementedError
