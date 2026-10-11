"""The analyser must never import the generator.

If the generator and the analyser are mirror images, a perfect decomposition
proves nothing -- and it edges toward the "answers hard-coded for demo or test
cases" disqualification condition. The analyser fits from the CSVs alone and
never reads a generator constant or ground_truth.json.

This is the one place that rule is enforced rather than merely intended.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "rng"

# Everything except rng/data sits on the far side of the wall. rng/eval is
# exempt: comparing an answer against ground truth is the whole point of it.
WALLED_OFF = ("analysis", "ingest", "graph", "api", "ui", "store")

IMPORTS_GENERATOR = re.compile(
    r"^\s*(?:from\s+(?:rng\.)?data[\s.]|import\s+rng\.data|from\s+\.\.data\s)",
    re.MULTILINE,
)
READS_GROUND_TRUTH = re.compile(r"ground_truth\.json")


def _offenders(pattern: re.Pattern) -> list[str]:
    found = []
    for package in WALLED_OFF:
        directory = PKG / package
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.py"):
            if pattern.search(path.read_text(encoding="utf-8")):
                found.append(str(path.relative_to(ROOT)))
    return found


def test_no_module_outside_rng_data_imports_the_generator():
    offenders = _offenders(IMPORTS_GENERATOR)
    assert not offenders, (
        "these modules import the generator, which makes any decomposition "
        f"result circular: {offenders}")


def test_no_module_outside_rng_data_reads_ground_truth():
    offenders = _offenders(READS_GROUND_TRUTH)
    assert not offenders, (
        "ground_truth.json is for the evaluation harness only; these modules "
        f"would be marking their own homework: {offenders}")
