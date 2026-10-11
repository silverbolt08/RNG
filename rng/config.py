"""Settings, and every number that is our choice rather than a fact.

Annex A requires the emission factor to be stated, and savings ranges rest on
assumptions that are not in the data at all. Those live here, in one place, so
they can be read straight into every response's `assumptions[]` instead of
being scattered through the code where a judge cannot find them.

Values the data pack supplies (emission factor, peak hours, reconciliation
tolerance) are read from `dataset_meta.json` when it is present and fall back
to these defaults when it is not -- which is what will happen when the judges
load a CSV of their own.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _flag(name: str, default: bool = False) -> bool:
    return _env(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    duckdb_path: Path
    schema_sql: Path
    llm_provider: str
    llm_model: str
    gemini_api_key: str | None
    mock_llm: bool
    api_base_url: str
    log_level: str


@dataclass(frozen=True)
class Assumptions:
    """Declared in every response. None of this is derived from the data."""

    # Supplied by the data pack where there is one; defaults otherwise.
    emission_factor_kg_co2e_per_kwh: float = 0.71
    currency: str = "INR"
    peak_start_hour: int = 9
    peak_end_hour: int = 21
    peak_applies_on_weekends: bool = False
    submeter_tolerance_pct: float = 6.0

    # Never supplied by anyone. Ours, and said out loud.
    recovery_fraction: tuple[float, float] = (0.60, 0.85)
    fix_cost: dict[str, tuple[float, float]] = field(default_factory=lambda: {
        "compressor_leak_repair": (45_000.0, 90_000.0),
        "idle_shutdown_schedule": (0.0, 25_000.0),
        "hvac_setpoint_change": (0.0, 15_000.0),
        "lighting_controls": (120_000.0, 220_000.0),
        "compressor_sequencing": (60_000.0, 140_000.0),
    })
    source: str = "defaults"

    def as_statements(self) -> list[str]:
        """The lines that go into AnalyzeResponse.assumptions."""
        peak = (f"{self.peak_start_hour:02d}:00-{self.peak_end_hour:02d}:00"
                f"{'' if self.peak_applies_on_weekends else ', weekdays only'}")
        return [
            f"Emission factor {self.emission_factor_kg_co2e_per_kwh} kg CO2e "
            f"per kWh ({self.source}).",
            f"Peak tariff hours taken as {peak} UTC; Annex B's tariff table "
            f"names the period but not the hours ({self.source}).",
            "All clock hours (shifts, peak window, night load) are read in "
            "UTC, the time zone of the meter timestamps; no site-local "
            "conversion is applied.",
            f"Recovery fraction {self.recovery_fraction[0]:.0%}-"
            f"{self.recovery_fraction[1]:.0%} of identified waste is "
            "actually recoverable (our assumption, not in the data).",
            "Fix costs are our estimates and are not present in any input "
            "file; payback ranges inherit their uncertainty.",
            f"Sub-meter totals are reconciled to the main meter within "
            f"{self.submeter_tolerance_pct}%.",
            f"All money in {self.currency}.",
        ]


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings(
        data_dir=Path(_env("DATA_DIR", "data/primary")),
        duckdb_path=Path(_env("DUCKDB_PATH", "rng.duckdb")),
        schema_sql=ROOT / "rng" / "store" / "schema.sql",
        llm_provider=_env("LLM_PROVIDER", "gemini"),
        llm_model=_env("LLM_MODEL", "gemini-3.5-flash-lite"),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        mock_llm=_flag("MOCK_LLM", True),
        api_base_url=_env("API_BASE_URL", "http://localhost:8000"),
        log_level=_env("LOG_LEVEL", "INFO"),
    )


@lru_cache(maxsize=1)
def assumptions() -> Assumptions:
    """Defaults, overridden by the data pack's dataset_meta.json if it exists."""
    meta_path = settings().data_dir / "dataset_meta.json"
    if not meta_path.is_file():
        return Assumptions()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    base = Assumptions()
    return Assumptions(
        emission_factor_kg_co2e_per_kwh=meta.get(
            "emission_factor_kg_co2e_per_kwh",
            base.emission_factor_kg_co2e_per_kwh),
        currency=meta.get("currency", base.currency),
        peak_start_hour=meta.get("peak_start_hour", base.peak_start_hour),
        peak_end_hour=meta.get("peak_end_hour", base.peak_end_hour),
        peak_applies_on_weekends=meta.get(
            "peak_applies_on_weekends", base.peak_applies_on_weekends),
        submeter_tolerance_pct=meta.get(
            "submeter_reconciliation_tolerance_pct",
            base.submeter_tolerance_pct),
        source=f"stated in {meta_path.name}",
    )
