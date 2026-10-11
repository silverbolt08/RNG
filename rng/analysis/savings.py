"""R8 savings estimate and R9 ranking. Owner: teammate C.

    estimate_savings(findings) -> list[Recommendation]

Every figure is a low and a high value (Annex A). kWh, money and CO2e ranges
come from the finding's energy and the assumption ranges in
rng.config.assumptions() -- recovery_fraction, fix_cost, the emission factor.
Fix costs are not in any input file: they are our assumption and must be
declared, which is why they live in config and not here.

No range is invented, and no number is written by the LLM. Every
recommendation carries at least one evidence_ref and requires_approval=True:
actions are suggestions, a manager approves them.

Ranking (R9): payback period first, then ease.
"""

from __future__ import annotations

from rng.api.schemas import Finding, Recommendation


def estimate_savings(findings: list[Finding]) -> list[Recommendation]:
    """Costed recommendations, ranked by payback then ease."""
    raise NotImplementedError
