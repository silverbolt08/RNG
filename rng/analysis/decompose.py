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

Ranges: refit the baseline on resamples, recompute the split on each, report
the 10th / 90th percentiles. No range is invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rng.analysis.baseline import BaselineModel
from rng.api.schemas import BridgeItem, Cause
from rng.ingest.clean import CleanTables

MODELLED_CAUSES = (Cause.TARIFF, Cause.VOLUME, Cause.PRODUCT_MIX, Cause.WEATHER)


@dataclass(frozen=True)
class Bridge:
    period: str
    compare_to: str
    reference_cost: float
    current_cost: float
    effects: dict[Cause, float]                    # currency; all five causes
    ranges_pp: dict[Cause, tuple[float, float]] = field(default_factory=dict)
    r2: float = 0.0

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


def decompose(clean: CleanTables, period: str, compare_to: str,
              model: BaselineModel) -> Bridge:
    """Split the bill change between `compare_to` and `period` into five causes."""
    raise NotImplementedError
