"""The four endpoints of the fixed contract (guide section 6)."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from rng.api.schemas import (
    AnalyzeRequest,
    AnalyzeResponse,
    ComponentHealth,
    HealthResponse,
    IngestResponse,
    TableName,
    TraceResponse,
)

router = APIRouter()
_TABLES = {t.value for t in TableName}
_ORDER = {"ok": 0, "degraded": 1, "down": 2}


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze(body: AnalyzeRequest, request: Request) -> AnalyzeResponse:
    return request.app.state.engine.answer(body)


@router.get("/trace/{run_id}", response_model=TraceResponse)
def trace(run_id: str, request: Request) -> TraceResponse:
    found = request.app.state.store.trace(run_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no run with id {run_id!r}")
    return found


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    state = request.app.state
    pipeline = getattr(state, "pipeline", {"ok": False, "detail": "not started"})
    try:
        counts = state.store.counts()
        tiers = pipeline.get("tiers", {})
        failed = sorted(t for t, s in tiers.items() if s != "ok")
        if not counts["meter_15min"]:
            store = ComponentHealth(status="degraded", detail=(
                "no data loaded: " + pipeline.get("detail", "load a dataset")))
        elif failed:
            store = ComponentHealth(status="degraded", detail=(
                f"{counts['meter_15min']} meter rows; analysis tiers not "
                f"available: {', '.join(failed)}"))
        else:
            store = ComponentHealth(status="ok", detail=(
                f"{counts['meter_15min']} meter rows, "
                f"{counts['analysis_cache']} cached results, "
                f"tiers: {', '.join(sorted(tiers)) or 'none'}"))
    except Exception as exc:
        store = ComponentHealth(status="down", detail=f"{type(exc).__name__}: {exc}")
    llm = state.llm.health()
    worst = max((store.status, llm.status), key=_ORDER.get)
    return HealthResponse(
        status=worst, api=ComponentHealth(status="ok"), data_store=store, llm=llm,
        dataset_id=pipeline.get("dataset_id"), mock_llm=state.llm.mock)


# -- POST /ingest -------------------------------------------------------------

def _table_from(text: str) -> str | None:
    """A table name given plainly, or inside a JSON object or string."""
    text = (text or "").strip()
    if text in _TABLES:
        return text
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(data, str):
        return data if data in _TABLES else None
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, str) and value in _TABLES:
                return value
    return None


def _ingest(app, table: str, upload: Path) -> IngestResponse:
    """A thin wrapper over the CLI loader: check the file with the same
    load_csv, swap it into the working copy, re-run the same precompute."""
    from rng.api.main import run_precompute
    from rng.ingest.loader import load_csv

    try:
        loaded = load_csv(upload, table)
    except Exception as exc:
        return IngestResponse(table=table, rows_loaded=0, rows_rejected=0,
                              status="failed", messages=[f"{type(exc).__name__}: {exc}"])
    if loaded.rows_loaded == 0:
        return IngestResponse(table=table, rows_loaded=0,
                              rows_rejected=loaded.rows_rejected, status="failed",
                              messages=[*loaded.messages, "no usable rows in the file"])

    target = app.state.live_dir / f"{table}.csv"
    backup = target.with_suffix(".csv.bak")
    if target.is_file():
        shutil.copyfile(target, backup)
    shutil.copyfile(upload, target)
    # The pack's own id no longer describes the data; let it be re-derived.
    (app.state.live_dir / "dataset_meta.json").unlink(missing_ok=True)

    status = run_precompute(app)
    messages = list(loaded.messages)
    if not status["ok"]:
        if backup.is_file():                       # put the old table back
            shutil.copyfile(backup, target)
            run_precompute(app)
        return IngestResponse(
            table=table, rows_loaded=0, rows_rejected=loaded.rows_rejected,
            status="failed", dataset_id=app.state.pipeline.get("dataset_id"),
            messages=[*messages, "the file loaded but the analysis could not "
                      f"be re-run with it, so it was not kept: {status['detail']}"])
    not_ok = sorted(t for t, s in status["tiers"].items() if s != "ok")
    if not_ok:
        messages.append("analysis re-run; tiers not available: " + ", ".join(not_ok))
    else:
        messages.append("analysis re-run on the new data")
    return IngestResponse(
        table=table, rows_loaded=loaded.rows_loaded,
        rows_rejected=loaded.rows_rejected,
        status="ok" if loaded.rows_rejected == 0 else "partial",
        dataset_id=status.get("dataset_id"), messages=messages)


@router.post("/ingest", response_model=IngestResponse)
async def ingest(request: Request) -> IngestResponse:
    """Multipart: a CSV file plus the table name.

    The guide fixes the content, not the field names, so this accepts the CSV
    under any field and the table name as a form field, a JSON form field
    such as {"table": "tariff"}, a JSON file part, a ?table= query parameter,
    or failing all of those the file's own name.
    """
    form = await request.form()
    table = _table_from(request.query_params.get("table", ""))
    csv_part = None
    for _, value in form.multi_items():
        if hasattr(value, "filename") and value.filename:
            if value.filename.lower().endswith(".json"):
                table = table or _table_from((await value.read()).decode("utf-8", "replace"))
            elif csv_part is None:
                csv_part = value
        else:
            table = table or _table_from(str(value))
    if csv_part is None:
        raise HTTPException(status_code=422, detail="no CSV file part in the request")
    table = table or _table_from(Path(csv_part.filename).stem)
    if table is None:
        raise HTTPException(status_code=422, detail=(
            "name the table: one of " + ", ".join(sorted(_TABLES))))

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{table}.csv"
        path.write_bytes(await csv_part.read())
        return await run_in_threadpool(_ingest, request.app, table, path)
