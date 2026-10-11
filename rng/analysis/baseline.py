"""R2 baseline. Owner: teammate C.

    kwh_day ~ b0 + sum_p(beta_p * units_p) + beta_cdd * CDD + beta_hdd * HDD

One coefficient per product family, so beta_p reads as "kWh per unit of
product p" -- explainable in the Q&A, and invertible for counterfactuals.

FIT ON THE REFERENCE PERIOD ONLY (CLAUDE.md section 9, #1). Fit on both
quarters and the leak becomes "normal": waste collapses to zero and the
controllable part of the bill disappears from the answer.

Fits from the cleaned tables alone. This package may never import rng.data
or read the generator's answer key -- tests/test_no_generator_import.py
enforces it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from rng.ingest.clean import CleanTables


@dataclass(frozen=True)
class BaselineModel:
    fitted_on: str                 # the period label it was fitted on
    intercept: float               # kWh per day
    beta_units: dict[str, float]   # product -> kWh per unit
    beta_cdd: float
    beta_hdd: float
    r2: float
    n_days: int

    def predict(self, units: dict[str, np.ndarray], cdd: np.ndarray,
                hdd: np.ndarray) -> np.ndarray:
        """Expected main-meter kWh per day."""
        raise NotImplementedError


def fit_baseline(clean: CleanTables, period: str) -> BaselineModel:
    """Fit on `period` (the reference quarter) and nothing else."""
    raise NotImplementedError
