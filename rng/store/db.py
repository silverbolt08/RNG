"""DuckDB connection and schema bootstrap. Owner: teammate B."""

from __future__ import annotations

import duckdb

from rng.config import settings


def connect(path: str | None = None) -> duckdb.DuckDBPyConnection:
    """Open the single-file store. Pass ':memory:' in tests.

    The session is pinned to UTC. DuckDB returns TIMESTAMPTZ values in the
    session time zone, which defaults to the machine's: on a laptop in India
    midnight UTC comes back as 05:30, `.hour` is then wrong by five and a half
    hours, the peak window lands on the wrong intervals and the tariff term --
    the largest in the bridge -- is silently mispriced. It would also differ
    between a laptop and the container.
    """
    con = duckdb.connect(str(path or settings().duckdb_path))
    con.execute("SET TimeZone = 'UTC'")
    return con


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create every table. Idempotent, so a reload through /ingest is safe."""
    con.execute(settings().schema_sql.read_text(encoding="utf-8"))
