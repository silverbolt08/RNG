"""Load, clean, store, and precompute every analysis tier. Owner: teammate B.

    precompute(data_dir) -> PrecomputeResult
    current()            -> Current(clean, model, dataset_id)

Called once at API startup and again by POST /ingest; scripts/load.py is the
CLI over the same function. Every answer afterwards is a cache lookup.

Each tier runs in its own try/except. A broken (or not yet implemented) bridge
must not stop asset totals from being cached: top-users questions only need
measured data, and the status of every tier is returned so /health can say
which ones are live.

Periods come from the data. Each quarter present is compared with the quarter
before it when that quarter is present too.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import duckdb
import pandas as pd

from rng.analysis.assets import asset_totals
from rng.analysis.baseline import BaselineModel, fit_baseline
from rng.analysis.decompose import decompose
from rng.analysis.savings import estimate_savings
from rng.analysis.waste import detect_leaks, find_idle_load
from rng.ingest.clean import CleaningReport, CleanTables, clean
from rng.ingest.loader import RawTables, load_dir
from rng.periods import available_periods, previous_period
from rng.store import cache
from rng.store.db import connect, init_schema

log = logging.getLogger(__name__)


class Current(NamedTuple):
    clean: CleanTables
    model: BaselineModel | None     # fitted on the latest reference period
    dataset_id: str


@dataclass
class PrecomputeResult:
    dataset_id: str
    periods: list[str]
    comparisons: list[tuple[str, str]]          # (period, compare_to)
    rows_loaded: int
    rows_rejected: int
    report: CleaningReport
    tiers: dict[str, str] = field(default_factory=dict)   # tier -> ok | error
    messages: list[str] = field(default_factory=list)


_current: Current | None = None


def current() -> Current:
    """The in-memory tables and model from the last precompute()."""
    if _current is None:
        raise RuntimeError("precompute() has not run yet")
    return _current


def _dataset_id(data_dir: Path) -> str:
    """dataset_meta.json's id when there is one, else a hash of the files."""
    meta = data_dir / "dataset_meta.json"
    if meta.is_file():
        found = json.loads(meta.read_text(encoding="utf-8")).get("dataset_id")
        if found:
            return str(found)
    digest = hashlib.sha1()
    for path in sorted(data_dir.glob("*.csv")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return f"{data_dir.name}-{digest.hexdigest()[:10]}"


def _replace(con: duckdb.DuckDBPyConnection, table: str, frame: pd.DataFrame,
             select: str) -> None:
    con.execute(f"DELETE FROM {table}")
    con.register("_frame", frame)
    try:
        con.execute(f"INSERT INTO {table} SELECT {select} FROM _frame")
    finally:
        con.unregister("_frame")


def _store_tables(con: duckdb.DuckDBPyConnection, raw: RawTables,
                  tables: CleanTables, dataset_id: str, source: str) -> None:
    tariff = raw.tariff.copy()
    for col in ("peak_start_hour", "peak_end_hour"):
        if col not in tariff.columns:
            tariff[col] = pd.NA
    _replace(con, "meter_15min", raw.meter_15min, "meter_id, ts, kwh")
    _replace(con, "tariff", tariff,
             "valid_from, valid_to, period, price_per_kwh, "
             "CAST(peak_start_hour AS INTEGER), CAST(peak_end_hour AS INTEGER)")
    _replace(con, "production", raw.production,
             "CAST(date AS DATE), product, units, shift_pattern, is_holiday")
    _replace(con, "weather", raw.weather,
             "CAST(date AS DATE), avg_temp_c, cooling_degree_days, heating_degree_days")
    _replace(con, "meter_clean", tables.meter, "meter_id, ts, kwh, imputed, reset_adj")
    counts = {t: r.rows_loaded for t, r in raw.loads.items()}
    counts["rejected"] = raw.rows_rejected
    con.execute("DELETE FROM datasets")
    con.execute("INSERT INTO datasets VALUES (?, ?, ?, ?)",
                [dataset_id, datetime.now(UTC), source, json.dumps(counts)])


def _tier(result: PrecomputeResult, name: str, fn) -> Any:
    """Run one tier; record ok / not implemented / error and never raise."""
    try:
        value = fn()
    except NotImplementedError:
        result.tiers.setdefault(name, "not implemented")
        return None
    except Exception as exc:                     # noqa: BLE001 -- isolate tiers
        log.exception("tier %s failed", name)
        result.tiers[name] = f"error: {type(exc).__name__}: {exc}"
        return None
    result.tiers.setdefault(name, "ok")
    return value


def _dump(items) -> list:
    return [i.model_dump(mode="json") for i in items]


def precompute(data_dir: str | Path,
               con: duckdb.DuckDBPyConnection | None = None) -> PrecomputeResult:
    """Load and clean `data_dir`, store it, and cache every tier that works."""
    global _current
    data_dir = Path(data_dir)
    raw = load_dir(data_dir)
    tables, report = clean(raw)
    dataset_id = _dataset_id(data_dir)

    own = con is None
    con = con or connect()
    try:
        init_schema(con)
        _store_tables(con, raw, tables, dataset_id, str(data_dir))
        con.execute("DELETE FROM analysis_cache WHERE dataset_id = ?", [dataset_id])

        periods = available_periods(tables.meter["ts"])
        comparisons = [(p, previous_period(p)) for p in periods
                       if previous_period(p) in periods]
        result = PrecomputeResult(
            dataset_id=dataset_id, periods=periods, comparisons=comparisons,
            rows_loaded=raw.rows_loaded, rows_rejected=raw.rows_rejected,
            report=report, messages=[m for r in raw.loads.values() for m in r.messages])

        def put(kind: str, period: str, payload: Any, compare_to: str | None = None):
            cache.put(con, dataset_id, kind, period, payload, compare_to)

        idle: dict[str, dict] = {}
        for period in periods:
            put("cleaning_report", period, asdict(report))
            _tier(result, "asset_totals", lambda p=period: put(
                "asset_totals", p, [asdict(a) for a in asset_totals(tables, p)]))
            stats = _tier(result, "idle_load", lambda p=period: find_idle_load(tables, p))
            if stats is not None:
                idle[period] = stats
                put("idle_load", period, {k: asdict(v) for k, v in stats.items()})

        models: dict[str, BaselineModel] = {}
        model = None
        for period, compare_to in comparisons:
            if compare_to not in models:
                fitted = _tier(result, "baseline", lambda r=compare_to: fit_baseline(tables, r))
                if fitted is None:
                    continue
                models[compare_to] = fitted
            model = models[compare_to]

            def _bridge(p=period, r=compare_to, m=model):
                b = decompose(tables, p, r, m)
                b.assert_adds_up()
                return b
            bridge = _tier(result, "bridge", _bridge)
            if bridge is None:
                continue
            put("bridge", period, {
                "period": bridge.period, "compare_to": bridge.compare_to,
                "reference_cost": bridge.reference_cost,
                "current_cost": bridge.current_cost,
                "total_pct": bridge.total_pct, "r2": bridge.r2,
                "items": _dump(bridge.to_items()),
            }, compare_to)

            if period not in idle:
                continue
            findings = _tier(result, "findings",
                             lambda b=bridge, p=period: detect_leaks(tables, b, idle[p]))
            if findings is None:
                continue
            put("findings", period, _dump(findings), compare_to)
            recs = _tier(result, "recommendations", lambda f=findings: estimate_savings(f))
            if recs is not None:
                put("recommendations", period, _dump(recs), compare_to)

        _current = Current(clean=tables, model=model, dataset_id=dataset_id)
        return result
    finally:
        if own:
            con.close()
