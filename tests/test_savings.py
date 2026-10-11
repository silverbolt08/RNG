"""Savings and ranking. Every input here is hand-made: savings.py sits behind
the import wall and only ever sees the Finding it is given."""

from __future__ import annotations

import pandas as pd
import pytest

from rng.analysis.savings import average_price, estimate_savings
from rng.api.schemas import Confidence, Ease, Finding
from rng.config import Assumptions

PRICE = 5.0
A = Assumptions()


def _finding(asset: str = "compressors", kind: str = "leak",
             kwh: float = 100_000.0, fid: str = "f1") -> Finding:
    return Finding(
        finding_id=fid, asset=asset, kind=kind,
        description=f"Night load on {asset} stays high",
        kwh_per_year=kwh, evidence_refs=[f"meter_15min:{asset}:2026-Q3"],
        confidence=Confidence.MEDIUM, detected_from="2026-08-03")


def test_ranges_follow_from_the_finding_and_the_declared_assumptions():
    rec, = estimate_savings([_finding(kwh=100_000.0)], price_per_kwh=PRICE)
    impact = rec.estimated_impact
    low, high = A.recovery_fraction
    cost_low, cost_high = A.fix_cost["compressor_leak_repair"]

    assert impact.saving_kwh_per_year == (100_000 * low, 100_000 * high)
    assert impact.saving_money_per_year == (100_000 * low * PRICE,
                                            100_000 * high * PRICE)
    factor = A.emission_factor_kg_co2e_per_kwh / 1000
    assert impact.saving_co2e_t_per_year == pytest.approx(
        (100_000 * low * factor, 100_000 * high * factor), abs=0.01)
    # Cheapest fix against the largest saving, dearest against the smallest.
    assert impact.payback_months == pytest.approx(
        (12 * cost_low / (100_000 * high * PRICE),
         12 * cost_high / (100_000 * low * PRICE)), abs=0.05)


def test_every_range_is_ordered_and_every_recommendation_has_evidence():
    findings = [_finding("compressors", "leak", 130_000, "f1"),
                _finding("lighting", "idle", 40_000, "f2"),
                _finding("hvac", "schedule", 25_000, "f3"),
                _finding("line_9", "idle", 60_000, "f4")]
    recs = estimate_savings(findings, price_per_kwh=PRICE)
    assert len(recs) == len(findings)
    for rec in recs:
        impact = rec.estimated_impact
        for low, high in (impact.saving_kwh_per_year, impact.saving_money_per_year,
                          impact.saving_co2e_t_per_year, impact.payback_months):
            assert 0 <= low <= high
        assert rec.evidence_refs
        assert rec.requires_approval is True
        assert rec.action and rec.rationale


def test_evidence_comes_from_the_finding_and_names_it():
    rec, = estimate_savings([_finding(fid="leak-7")], price_per_kwh=PRICE)
    assert "meter_15min:compressors:2026-Q3" in rec.evidence_refs
    assert "finding:leak-7" in rec.evidence_refs
    assert rec.confidence is Confidence.MEDIUM


def test_ranked_by_payback_first():
    # Same fix, so the bigger saving pays back sooner and must come first.
    recs = estimate_savings([_finding(kwh=20_000, fid="small"),
                             _finding(kwh=200_000, fid="big")],
                            price_per_kwh=PRICE)
    assert "finding:big" in recs[0].evidence_refs
    mids = [sum(r.estimated_impact.payback_months) / 2 for r in recs]
    assert mids == sorted(mids)


def test_ease_breaks_a_payback_tie(monkeypatch):
    same_cost = {k: (10_000.0, 20_000.0) for k in A.fix_cost}
    monkeypatch.setattr("rng.analysis.savings.assumptions",
                        lambda: Assumptions(fix_cost=same_cost))
    recs = estimate_savings([_finding("lighting", "idle", 50_000, "hard"),
                             _finding("line_9", "idle", 50_000, "easy")],
                            price_per_kwh=PRICE)
    assert [r.estimated_impact.ease for r in recs] == [Ease.EASY, Ease.HARD]


def test_an_unknown_asset_still_gets_a_recommendation():
    """No meter name is assumed: an unrecognised asset gets the generic fix."""
    rec, = estimate_savings([_finding("zz_meter_42", "idle", 50_000)],
                            price_per_kwh=PRICE)
    assert "zz_meter_42" in rec.action
    low, high = A.fix_cost["idle_shutdown_schedule"]
    assert f"{low:,.0f}-{high:,.0f}" in rec.rationale


def test_nothing_found_means_nothing_recommended():
    assert estimate_savings([]) == []
    assert estimate_savings([_finding(kwh=0.0)], price_per_kwh=PRICE) == []


def test_a_saving_is_never_priced_at_zero():
    with pytest.raises(ValueError):
        estimate_savings([_finding()], price_per_kwh=0.0)


def _tariff(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["valid_from", "valid_to", "period",
                                       "price_per_kwh", "peak_start_hour",
                                       "peak_end_hour"])


def test_average_price_weights_peak_hours_at_the_latest_tariff():
    tariff = _tariff([
        ("2026-04-01T00:00:00Z", "2026-07-01T00:00:00Z", "peak", 100.0, 9, 21),
        ("2026-04-01T00:00:00Z", "2026-07-01T00:00:00Z", "off_peak", 50.0, 9, 21),
        ("2026-07-01T00:00:00Z", "2026-10-01T00:00:00Z", "peak", 8.0, 9, 21),
        ("2026-07-01T00:00:00Z", "2026-10-01T00:00:00Z", "off_peak", 4.0, 9, 21),
    ])
    # 12 peak hours on 5 weekdays out of 168 hours; the old window is ignored.
    peak_share = 12 * 5 / 168
    assert average_price(tariff) == pytest.approx(
        8.0 * peak_share + 4.0 * (1 - peak_share))


def test_average_price_of_a_flat_tariff_is_that_price():
    tariff = pd.DataFrame({"valid_from": ["2026-04-01"], "valid_to": ["2026-10-01"],
                           "period": ["flat"], "price_per_kwh": [6.5]})
    assert average_price(tariff) == pytest.approx(6.5)


def test_the_price_falls_back_to_the_configured_tariff_file():
    """How the precompute pipeline calls it: findings only."""
    rec, = estimate_savings([_finding()])
    low, high = rec.estimated_impact.saving_money_per_year
    assert 0 < low < high
