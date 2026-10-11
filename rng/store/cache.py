"""analysis_cache read/write. Owner: teammate B.

Everything is precomputed at startup and served from here, so every answer is
a lookup. The keys differ by kind on purpose:

    asset_totals, idle_load   (dataset_id, period)              compare_to NULL
    bridge, findings, savings (dataset_id, period, compare_to)

One blob keyed by (period, compare_to) would break "which assets used the most
in Q3?" by forcing a comparison period the question never asked for.

A single-period result has no compare_to. Store it as '' (the column is part
of the primary key and DuckDB keys reject NULL) and hand callers None.
Write with INSERT OR REPLACE so a reload through /ingest overwrites cleanly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import duckdb


def _key(compare_to: str | None) -> str:
    return "" if compare_to is None else compare_to


def put(con: duckdb.DuckDBPyConnection, dataset_id: str, kind: str, period: str,
        payload: dict[str, Any], compare_to: str | None = None) -> None:
    # Some payloads are lists (asset_totals, findings); anything JSON-able goes.
    con.execute(
        "INSERT OR REPLACE INTO analysis_cache "
        "(dataset_id, kind, period, compare_to, payload, computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [dataset_id, kind, period, _key(compare_to),
         json.dumps(payload, default=str), datetime.now(UTC)])


def get(con: duckdb.DuckDBPyConnection, dataset_id: str, kind: str, period: str,
        compare_to: str | None = None) -> dict[str, Any] | None:
    """The cached payload, or None if it was never computed."""
    row = con.execute(
        "SELECT payload FROM analysis_cache "
        "WHERE dataset_id = ? AND kind = ? AND period = ? AND compare_to = ?",
        [dataset_id, kind, period, _key(compare_to)]).fetchone()
    return None if row is None else json.loads(row[0])
