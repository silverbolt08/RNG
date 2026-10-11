-- DuckDB schema. FROZEN.
--
-- This file and api/schemas.py are the only two files shared by all four
-- workstreams. Change either one by agreement only.
--
-- Run it with db.execute(Path("rng/store/schema.sql").read_text()) at startup.
-- Every statement is idempotent, so a reload through POST /ingest is safe.
--
-- JSON-shaped columns are VARCHAR holding json.dumps() output rather than
-- DuckDB's JSON type: nothing queries inside them, and VARCHAR cannot fail on
-- an extension that did not load on the demo machine.

-- =========================================================================
-- Raw tables -- exactly the Annex B schema
--
-- Extra columns on input are allowed and are preserved where we rely on them
-- (peak_start_hour / peak_end_hour), because Annex B's tariff table says
-- which PERIOD applies but never which HOURS are peak. A file without them
-- falls back to the convention stated in rng/config.py and declared in every
-- response's assumptions[].
-- =========================================================================

CREATE TABLE IF NOT EXISTS meter_15min (
    meter_id         VARCHAR     NOT NULL,
    ts               TIMESTAMPTZ NOT NULL,
    kwh              DOUBLE      NOT NULL
);

CREATE TABLE IF NOT EXISTS tariff (
    valid_from       TIMESTAMPTZ NOT NULL,
    valid_to         TIMESTAMPTZ NOT NULL,
    period           VARCHAR     NOT NULL,   -- peak | off_peak | flat
    price_per_kwh    DOUBLE      NOT NULL,
    peak_start_hour  INTEGER,
    peak_end_hour    INTEGER
);

CREATE TABLE IF NOT EXISTS production (
    date             DATE        NOT NULL,
    product          VARCHAR     NOT NULL,
    units            BIGINT      NOT NULL,
    shift_pattern    VARCHAR,
    is_holiday       BOOLEAN
);

CREATE TABLE IF NOT EXISTS weather (
    date                 DATE   NOT NULL,
    avg_temp_c           DOUBLE,
    cooling_degree_days  DOUBLE,
    heating_degree_days  DOUBLE
);

-- =========================================================================
-- System tables
-- =========================================================================

-- One row in normal operation. Keeps the cache key honest and records the row
-- counts the Annex D data card has to state.
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id   VARCHAR PRIMARY KEY,
    loaded_at    TIMESTAMPTZ NOT NULL,
    source       VARCHAR,
    row_counts   VARCHAR            -- json: {"meter_15min": 157968, ...}
);

-- Cleaned meter data. The flags are kept rather than dropped so the demo can
-- show what cleaning actually did.
CREATE TABLE IF NOT EXISTS meter_clean (
    meter_id     VARCHAR     NOT NULL,
    ts           TIMESTAMPTZ NOT NULL,
    kwh          DOUBLE      NOT NULL,
    imputed      BOOLEAN     NOT NULL DEFAULT FALSE,
    reset_adj    BOOLEAN     NOT NULL DEFAULT FALSE
);

-- Precomputed analysis, written once at startup and read on every request.
-- compare_to is NULLABLE on purpose: top-users and idle-load are keyed by a
-- single period, and forcing them to carry an arbitrary comparison period
-- would break questions that never asked for one.
CREATE TABLE IF NOT EXISTS analysis_cache (
    dataset_id   VARCHAR     NOT NULL,
    kind         VARCHAR     NOT NULL,   -- asset_totals | idle_load | bridge | ...
    period       VARCHAR     NOT NULL,
    compare_to   VARCHAR,
    payload      VARCHAR     NOT NULL,   -- json
    computed_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (dataset_id, kind, period, compare_to)
);

-- R12. One row per request.
CREATE TABLE IF NOT EXISTS runs (
    run_id       VARCHAR PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL,
    question     VARCHAR     NOT NULL,
    answer_type  VARCHAR,
    request      VARCHAR,                -- json
    response     VARCHAR,                -- json
    llm_calls    INTEGER     NOT NULL DEFAULT 0,
    tokens       INTEGER     NOT NULL DEFAULT 0,
    latency_ms   DOUBLE
);

-- Annex C's trace shape, one row per step.
CREATE TABLE IF NOT EXISTS run_steps (
    run_id   VARCHAR NOT NULL,
    seq      INTEGER NOT NULL,
    agent    VARCHAR NOT NULL,
    tool     VARCHAR NOT NULL,
    input    VARCHAR,                    -- json
    output   VARCHAR,                    -- json
    ms       DOUBLE,
    PRIMARY KEY (run_id, seq)
);

-- The number registry. The writer LLM sees these rows and nothing else, and
-- every numeral in the generated brief is matched back against them.
CREATE TABLE IF NOT EXISTS registry_values (
    run_id        VARCHAR NOT NULL,
    key           VARCHAR NOT NULL,
    value         DOUBLE  NOT NULL,
    unit          VARCHAR NOT NULL,
    source_tool   VARCHAR NOT NULL,
    evidence_ref  VARCHAR NOT NULL,
    PRIMARY KEY (run_id, key)
);

-- =========================================================================
-- Indexes
-- =========================================================================

CREATE INDEX IF NOT EXISTS idx_meter_ts        ON meter_15min (ts);
CREATE INDEX IF NOT EXISTS idx_meter_id_ts     ON meter_15min (meter_id, ts);
CREATE INDEX IF NOT EXISTS idx_clean_id_ts     ON meter_clean (meter_id, ts);
CREATE INDEX IF NOT EXISTS idx_production_date ON production (date);
CREATE INDEX IF NOT EXISTS idx_weather_date    ON weather (date);
CREATE INDEX IF NOT EXISTS idx_run_steps_run   ON run_steps (run_id);
CREATE INDEX IF NOT EXISTS idx_registry_run    ON registry_values (run_id);
