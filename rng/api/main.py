"""FastAPI application.

    uvicorn rng.api.main:app --host 0.0.0.0 --port 8000

Startup loads the data and precomputes every analysis tier, so each request
afterwards is a cache lookup plus at most three LLM calls.
"""

from __future__ import annotations

import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from rng.api.routes import router
from rng.config import settings
from rng.graph.graph import Engine
from rng.graph.llm import LLM
from rng.graph.store_io import Store
from rng.store.db import connect, init_schema

log = logging.getLogger("rng.api")
TABLES = ("meter_15min", "tariff", "production", "weather")


def prepare_live_dir(source: Path, live: Path) -> Path:
    """A working copy of the data pack that POST /ingest may overwrite.

    The pack under DATA_DIR is never modified: an upload replaces one table in
    this copy and the analysis is re-run from it.
    """
    live.mkdir(parents=True, exist_ok=True)
    for name in (*[f"{t}.csv" for t in TABLES], "dataset_meta.json"):
        src = source / name
        if src.is_file():
            shutil.copyfile(src, live / name)
    return live


def run_precompute(app: FastAPI) -> dict:
    """Load -> validate -> clean -> precompute. Never raises: a failure here
    is reported by /health, it does not stop the API from starting."""
    try:
        from rng.ingest.pipeline import precompute

        with app.state.store.exclusive() as con:
            result = precompute(app.state.live_dir, con)
        status = {"ok": True, "dataset_id": result.dataset_id,
                  "periods": result.periods, "tiers": dict(result.tiers),
                  "rows_loaded": result.rows_loaded,
                  "rows_rejected": result.rows_rejected}
    except Exception as exc:
        log.exception("precompute failed")
        status = {"ok": False, "detail": f"{type(exc).__name__}: {exc}", "tiers": {}}
    app.state.pipeline = status
    return status


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = settings()
    logging.basicConfig(level=cfg.log_level)
    con = connect()
    init_schema(con)
    app.state.store = Store(con)
    app.state.llm = LLM(cfg)
    app.state.engine = Engine(app.state.store, app.state.llm)
    app.state.live_dir = prepare_live_dir(
        cfg.data_dir, cfg.duckdb_path.resolve().parent / "live_data")
    status = run_precompute(app)
    log.info("startup: %s", status)
    yield
    con.close()


app = FastAPI(
    title="Factory Energy Cost Decomposition and Savings Agent",
    description="Splits a change in the electricity bill into tariff, volume, "
                "product mix, weather and waste, then recommends costed actions. "
                "Every number comes from code; the LLM only routes and writes.",
    version="1.0.0", lifespan=lifespan)
app.include_router(router)
