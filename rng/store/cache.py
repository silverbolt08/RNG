"""analysis_cache read/write. Owner: teammate B.

Everything is precomputed at startup and served from here, so every answer is
a lookup. The keys differ by kind on purpose:

    asset_totals, idle_load   (dataset_id, period)              compare_to NULL
    bridge, findings, savings (dataset_id, period, compare_to)

One blob keyed by (period, compare_to) would break "which assets used the most
in Q3?" by forcing a comparison period the question never asked for.
"""

from __future__ import annotations

from typing import Any

import duckdb


def put(con: duckdb.DuckDBPyConnection, dataset_id: str, kind: str, period: str,
        payload: dict[str, Any], compare_to: str | None = None) -> None:
    raise NotImplementedError


def get(con: duckdb.DuckDBPyConnection, dataset_id: str, kind: str, period: str,
        compare_to: str | None = None) -> dict[str, Any] | None:
    """The cached payload, or None if it was never computed."""
    raise NotImplementedError
