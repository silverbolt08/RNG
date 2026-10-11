"""Everything the graph reads from or writes to DuckDB.

Reads the precomputed analysis (written at startup by the ingest pipeline) and
persists the trace of every run: runs, run_steps, registry_values (R12).

One connection, one lock: DuckDB connections are not safe to share across
FastAPI's worker threads without one.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import duckdb

from rng.api.schemas import RegistryValue, TraceResponse, TraceStep
from rng.periods import available_periods


class Store:
    def __init__(self, con: duckdb.DuckDBPyConnection):
        self._con = con
        self._lock = threading.RLock()

    def _all(self, sql: str, params: list | None = None) -> list[tuple]:
        with self._lock:
            return self._con.execute(sql, params or []).fetchall()

    @contextmanager
    def exclusive(self):
        """The raw connection, held under the lock -- for the ingest pipeline,
        which rewrites the tables the request handlers read."""
        with self._lock:
            yield self._con

    # -- what the data contains -------------------------------------------

    def dataset_id(self) -> str | None:
        rows = self._all("SELECT dataset_id FROM datasets ORDER BY loaded_at DESC LIMIT 1")
        if not rows:
            rows = self._all("SELECT dataset_id FROM analysis_cache "
                             "ORDER BY computed_at DESC LIMIT 1")
        return rows[0][0] if rows else None

    def periods(self) -> list[str]:
        """Quarter labels actually present. Never assumed."""
        rows = self._all("SELECT DISTINCT CAST(date AS VARCHAR) FROM production")
        if not rows:
            rows = self._all("SELECT DISTINCT CAST(CAST(ts AS DATE) AS VARCHAR) "
                             "FROM meter_15min")
        if not rows:
            rows = [(p,) for (p,) in self._all(
                "SELECT DISTINCT period FROM analysis_cache")]
            return sorted(r[0] for r in rows)
        return available_periods([r[0] for r in rows])

    def assets(self) -> list[str]:
        rows = self._all("SELECT DISTINCT meter_id FROM meter_15min ORDER BY 1")
        return [r[0] for r in rows]

    def counts(self) -> dict[str, int]:
        out = {}
        for table in ("meter_15min", "tariff", "production", "weather",
                      "meter_clean", "analysis_cache", "runs"):
            out[table] = int(self._all(f"SELECT count(*) FROM {table}")[0][0])
        return out

    def cached(self, kind: str, period: str,
               compare_to: str | None = None) -> Any | None:
        """A precomputed result, or None if that tier was never computed."""
        dataset = self.dataset_id()
        if dataset is None:
            return None
        rows = self._all(
            "SELECT payload FROM analysis_cache WHERE dataset_id = ? AND kind = ? "
            "AND period = ? AND compare_to = ?",
            [dataset, kind, period, compare_to or ""])
        return json.loads(rows[0][0]) if rows else None

    # -- the trace ---------------------------------------------------------

    def save_run(self, *, run_id: str, question: str, answer_type: str,
                 request: dict, response: dict, llm_calls: int, tokens: int,
                 latency_ms: float, steps: list[dict],
                 registry: list[RegistryValue]) -> None:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, datetime.now(UTC), question, answer_type,
                 json.dumps(request), json.dumps(response), llm_calls, tokens,
                 latency_ms])
            for seq, step in enumerate(steps, start=1):
                extra = {k: v for k, v in step.items()
                         if k not in ("agent", "tool", "input", "output", "ms")}
                output = dict(step.get("output") or {})
                if extra:
                    output["_extra"] = extra
                self._con.execute(
                    "INSERT OR REPLACE INTO run_steps VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [run_id, seq, step["agent"], step["tool"],
                     json.dumps(step.get("input"), default=str),
                     json.dumps(output, default=str), step.get("ms")])
            for row in registry:
                self._con.execute(
                    "INSERT OR REPLACE INTO registry_values VALUES (?, ?, ?, ?, ?, ?)",
                    [run_id, row.key, row.value, row.unit, row.source_tool,
                     row.evidence_ref])

    def trace(self, run_id: str) -> TraceResponse | None:
        runs = self._all(
            "SELECT question, answer_type, llm_calls, tokens, latency_ms "
            "FROM runs WHERE run_id = ?", [run_id])
        if not runs:
            return None
        question, answer_type, llm_calls, tokens, latency_ms = runs[0]
        steps = []
        for seq, agent, tool, inp, out, ms in self._all(
                "SELECT seq, agent, tool, input, output, ms FROM run_steps "
                "WHERE run_id = ? ORDER BY seq", [run_id]):
            output = json.loads(out) if out else None
            extra = (output or {}).pop("_extra", {}) if isinstance(output, dict) else {}
            steps.append(TraceStep(
                seq=seq, agent=agent, tool=tool,
                input=json.loads(inp) if inp and inp != "null" else None,
                output=output or None, ms=ms, check=extra.get("check")))
        registry = [RegistryValue(key=k, value=v, unit=u, source_tool=s,
                                  evidence_ref=e)
                    for k, v, u, s, e in self._all(
                        "SELECT key, value, unit, source_tool, evidence_ref "
                        "FROM registry_values WHERE run_id = ? ORDER BY key",
                        [run_id])]
        return TraceResponse(run_id=run_id, question=question,
                             answer_type=answer_type, steps=steps,
                             llm_calls=llm_calls, tokens=tokens,
                             latency_ms=latency_ms, registry=registry)
