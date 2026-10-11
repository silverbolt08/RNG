"""Load a data directory and fill the analysis cache.

    python scripts/load.py data/primary [--db rng.duckdb]

The same code path as API startup and POST /ingest: rng.ingest.pipeline.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rng.ingest.pipeline import precompute  # noqa: E402
from rng.store.db import connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--db", help="DuckDB file (default: DUCKDB_PATH or rng.duckdb)")
    args = parser.parse_args(argv)

    con = connect(args.db)
    try:
        result = precompute(args.data_dir, con)
        cached = con.execute(
            "SELECT kind, count(*) FROM analysis_cache WHERE dataset_id = ? "
            "GROUP BY kind ORDER BY kind", [result.dataset_id]).fetchall()
    finally:
        con.close()

    r = result.report
    print(f"dataset   {result.dataset_id}")
    print(f"rows      {result.rows_loaded} loaded, {result.rows_rejected} rejected")
    print(f"periods   {', '.join(result.periods)}")
    print(f"compare   {', '.join(f'{p} vs {c}' for p, c in result.comparisons) or '-'}")
    print(f"cleaning  {len(r.gaps)} gaps ({r.intervals_imputed} intervals imputed), "
          f"{r.resets_adjusted} resets, {r.duplicates_dropped} duplicates, "
          f"reconciliation gap {r.reconciliation_gap_pct}%, "
          f"suspected holidays {r.suspected_holidays or 'none'}")
    for message in result.messages:
        print(f"note      {message}")
    print("tiers")
    for tier, status in result.tiers.items():
        print(f"  {tier:<16} {status}")
    print("cache")
    for kind, n in cached:
        print(f"  {kind:<16} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
