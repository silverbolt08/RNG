"""The number registry -- the faithfulness mechanism.

Every figure a response may quote is written here before the writer runs:

    reg.put("bridge.tariff.pp", 7.38, "pp", src="decompose_change",
            ev="bridge:tariff:2026-Q3", label="Tariff effect")

One table, three jobs:
  1. It is the ONLY thing the writer LLM sees. No database, no dataframes.
  2. It is the checklist: after generation every numeral in the prose is
     matched back against it. Unmatched -> regenerate once -> template.
  3. It is the evidence trail: it is stored per run and returned by /trace.

Worst case is stilted prose. A wrong number cannot leave the verify node.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rng.api.schemas import RegistryValue

# Things that contain digits but are not figures: period labels, dates, times.
_NOT_NUMBERS = (
    re.compile(r"\b20\d{2}-Q[1-4]\b", re.IGNORECASE),
    re.compile(r"\bQ[1-4]\b", re.IGNORECASE),
    re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b"),
    re.compile(r"\b\d{1,2}:\d{2}\b"),
)
# A numeral: optional sign, digits with thousands separators, optional decimals.
# Not preceded by a letter or underscore (CO2e, line_1) and not an ordinal.
_NUMERAL = re.compile(
    r"(?<![A-Za-z_\d.])[-+−]?\d[\d,]*(?:\.\d+)?(?!(?:st|nd|rd|th)\b)")

_FORMATS = {
    "pp": "{:.1f}", "%": "{:.1f}", "share_pct": "{:.1f}", "ratio": "{:.2f}",
    "r2": "{:.2f}", "kWh": "{:,.0f}", "kW": "{:.1f}", "hours": "{:,.0f}",
    "months": "{:.1f}", "tCO2e": "{:,.1f}", "count": "{:.0f}",
}
# How a unit reads in the table handed to the writer. Internal unit codes must
# not leak into prose ("36.8 share_pct").
_UNIT_WORDS = {"pp": "percentage points", "share_pct": "%", "ratio": "(ratio)",
               "r2": "", "count": "", "tCO2e": "tonnes CO2e"}


@dataclass(frozen=True)
class Entry:
    key: str
    value: float
    unit: str
    source_tool: str
    evidence_ref: str
    label: str

    @property
    def display(self) -> str:
        """The value exactly as the writer should quote it."""
        return _FORMATS.get(self.unit, "{:,.0f}").format(self.value)


class Registry:
    def __init__(self) -> None:
        self._entries: dict[str, Entry] = {}
        self._names: set[str] = set()

    # -- writing -----------------------------------------------------------

    def put(self, key: str, value: float, unit: str, *, src: str, ev: str,
            label: str = "") -> None:
        self._entries[key] = Entry(key, float(value), unit, src, ev,
                                   label or key)

    def name(self, *names: str) -> None:
        """Register identifiers that contain digits (line_1) so they are not
        mistaken for figures."""
        self._names.update(n for n in names if n)

    # -- reading -----------------------------------------------------------

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def value(self, key: str) -> float:
        return self._entries[key].value

    def show(self, key: str, absolute: bool = False) -> str:
        entry = self._entries[key]
        if absolute and entry.value < 0:
            entry = Entry(entry.key, abs(entry.value), entry.unit,
                          entry.source_tool, entry.evidence_ref, entry.label)
        return entry.display

    def entries(self) -> list[Entry]:
        return list(self._entries.values())

    def rows(self) -> list[RegistryValue]:
        return [RegistryValue(key=e.key, value=e.value, unit=e.unit,
                              source_tool=e.source_tool,
                              evidence_ref=e.evidence_ref)
                for e in self._entries.values()]

    def evidence_refs(self) -> list[str]:
        return sorted({e.evidence_ref for e in self._entries.values()})

    def facts_table(self) -> str:
        """What the writer LLM is given. Nothing else reaches it."""
        return "\n".join(
            f"- {e.label}: {e.display} {_UNIT_WORDS.get(e.unit, e.unit)}".rstrip()
            for e in self._entries.values() if e.unit != "quoted")

    # -- the check ---------------------------------------------------------

    def numerals(self, prose: str) -> list[tuple[str, float, int]]:
        """Every figure in the prose as (text, value, decimals shown)."""
        text = prose
        for pattern in _NOT_NUMBERS:
            text = pattern.sub(" ", text)
        for name in sorted(self._names, key=len, reverse=True):
            for variant in {name, name.replace("_", " ")}:
                text = re.sub(re.escape(variant), " ", text, flags=re.IGNORECASE)
        found = []
        for match in _NUMERAL.finditer(text):
            raw = match.group().rstrip(",")
            cleaned = raw.replace(",", "").replace("−", "-")
            decimals = len(cleaned.split(".")[1]) if "." in cleaned else 0
            found.append((raw, float(cleaned), decimals))
        return found

    def unmatched(self, prose: str) -> list[str]:
        """Numerals in the prose that no registry value explains.

        A numeral written to d decimals matches a value that rounds to it at
        d decimals -- the model writes "8.1" for 8.0999 and string equality
        would fight that all day. Sign is ignored: prose says "fell by 5.0".
        """
        values = [abs(e.value) for e in self._entries.values()]
        missing = []
        for raw, number, decimals in self.numerals(prose):
            tolerance = 0.5 * 10 ** (-decimals) + 1e-9
            if not any(abs(abs(number) - v) <= tolerance for v in values):
                missing.append(raw)
        return missing
