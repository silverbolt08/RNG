# Work plan — who builds what

Written 14:25 on 11 October. **Code freeze 16:15.** This file supersedes the
ownership table in `CLAUDE.md` §7 and the "next steps" in §13. Everything else
in `CLAUDE.md` (architecture, analytical method, guardrails) still stands.

Fill in the names:

| Role | Name | Area |
|---|---|---|
| **Owner** | | Orchestration, API, packaging, integration |
| **A** | | Validation, savings, evaluation, UI |
| **B** | | Data path: load, price, clean, cache, precompute |
| **C** | | Analytics core: baseline, bridge, waste, what-if |

---

# Part 1 — Master plan

## What already exists (on branch `data/generator-and-ground-truth`)

- `rng/data/generator.py`, `data/primary/` (15.00% rise) and `data/holdout/` (22%), each with `ground_truth.json` and `dataset_meta.json`.
- **Frozen contract:** `rng/api/schemas.py`, `rng/store/schema.sql`.
- **Shared helpers:** `rng/config.py` (settings and declared assumptions), `rng/periods.py` (period labels), `rng/store/db.py` (connection, pinned to UTC).
- **Stubs** with the final signatures for every module below. Your job is to replace `raise NotImplementedError`, not to change the signature.
- `requirements.txt`, `.env.example`, `pyproject.toml`, 31 passing tests, ruff clean.

## Step 0 — before anyone starts (14:30)

1. **Owner** pushes the branch and merges it to `main`.
2. **Everyone:** `git checkout main && git pull`, then `pip install -r requirements.txt`, then copy `.env.example` to `.env`.
3. **Everyone:** `python -m pytest` must show 31 passed before you write a line.
4. **Owner** installs Docker Desktop now if no teammate already has Docker running. It can need a restart, so it cannot wait until 16:00.

## How the pieces connect

```
CSV files ──B: load_dir──► RawTables ──B: clean──► CleanTables
                                                      │
              B: resolve_prices(tariff, ts) ◄─────────┤ (used by C and B)
                                                      ▼
   B: asset_totals      C: fit_baseline ─► C: decompose ─► Bridge
   C: find_idle_load                                         │
                          C: detect_leaks(clean, bridge, idle) ─► Finding[]
                                                                    │
                                        A: estimate_savings ─► Recommendation[]
                                                      │
              B: precompute() writes every result to analysis_cache
                                                      ▼
   Owner: POST /analyze ─► classify ─► guard ─► load_analysis ─► handler
                          ─► write_brief ─► verify ─► respond + trace
                                                      ▼
                         A: Streamlit UI and eval runner call the API over HTTP
```

## The seams — agree these and nothing else needs discussing

| Producer | Hands over | Consumer |
|---|---|---|
| B | `CleanTables` from `clean(raw)` | C, and B's own `asset_totals` |
| B | `resolve_prices(tariff, ts) -> (prices, convention)` | C (`decompose`, idle cost) |
| C | `Bridge` from `decompose(clean, period, compare_to, model)` | B (`precompute`), C (`detect_leaks`, what-if) |
| C | `list[Finding]` from `detect_leaks(clean, bridge, idle)` | A (`estimate_savings`) |
| A | `list[Recommendation]` from `estimate_savings(findings)` | B (`precompute`) |
| B | `analysis_cache` rows, and `pipeline.current()` | Owner (graph handlers) |
| Owner | `POST /analyze`, `/health`, `/trace/{run_id}`, `/ingest` | A (UI, eval), judges |

**Cache payloads (B writes, Owner reads).** One row per `kind`. The payload is the JSON of the contract type: `dataclasses.asdict()` for dataclasses, `.model_dump(mode="json")` for Pydantic models.

| `kind` | Key | Payload |
|---|---|---|
| `asset_totals` | period | list of `AssetTotal` |
| `idle_load` | period | dict of asset → `IdleStats` |
| `bridge` | period + compare_to | `period`, `compare_to`, `reference_cost`, `current_cost`, `total_pct`, `r2`, and `items` = `Bridge.to_items()` dumped |
| `findings` | period + compare_to | list of `Finding` |
| `recommendations` | period + compare_to | list of `Recommendation`, already ranked |
| `cleaning_report` | period | `CleaningReport` |

`pipeline.current()` returns the in-memory `clean`, `model` and `dataset_id` from the last precompute, so the what-if handler can re-run `decompose` on request.

## Rules

1. **One owner per file.** Never edit a file that is not in your list. If you need a change in someone else's file, ask them.
2. **Frozen:** `rng/api/schemas.py`, `rng/store/schema.sql`, `rng/config.py`, `rng/periods.py`, `requirements.txt`. Changes go through the Owner only.
3. **Signatures in the stubs are final.** If one truly cannot work, tell the consumer in the seams table before changing it.
4. **Do not wait for upstream.** Build against the stub signature and hand-made inputs; swap in the real thing when it merges.
5. **Nothing hard-coded.** No meter names, no meter counts, no `2026-Q3`. Use `rng.periods` and read what the data contains.
6. **`rng/analysis`, `rng/ingest`, `rng/graph`, `rng/api`, `rng/ui`, `rng/store` never import `rng.data` and never read `ground_truth.json`.** Only `rng/eval` may. A test enforces this.
7. **Open DuckDB only through `rng.store.db.connect()`.** All clock hours are UTC.
8. **The LLM never computes a number.** Every figure in a response comes from code and is in the registry.
9. **Branches:** `yourname/what-it-does`, small PRs, merge as soon as `python -m pytest && python -m ruff check rng tests` passes. Pull `main` before each new task.
10. **Each person writes tests in their own file** (`tests/test_<area>.py`) and commits under their own git identity.

## Checkpoints

| Time | What must be true | If it is not |
|---|---|---|
| **15:05** | First module from each person merged: B `tariff` + `loader`; C `baseline`; A `validate` + `savings`; Owner `registry` + mock LLM | Say so out loud. Do not hide a blocker. |
| **15:35** | One question answered end to end on `main` with `MOCK_LLM=true` | Owner wires top-users first: it needs only B's work |
| **15:55** | **Feature stop.** Only fixes after this. Eval has been run, validation output and three traces are saved | Apply the cut lines below |
| **16:05** | `docker compose up` works from a clean clone. Tag `final` | Fix Docker before anything else |
| **16:15** | Freeze | |

## Cut lines — drop in this order if behind

1. Bootstrap with 50 resamples → use 15. Ranges must still exist; the schema requires them.
2. Shapley versus Laspeyres comparison over 50 scenarios → state it as future work.
3. Step-change dating of the leak → report the finding without a start date.
4. UI extras → keep the question box, the summary, the bridge chart and the recommendations table.
5. Real Gemini classifier → keep the keyword router, but the **writer must be a real LLM call** behind `LLM_PROVIDER` / `LLM_MODEL`.

Never cut: the bridge adding up, the evaluation (its absence disqualifies), `/analyze` `/health` `/trace` `/ingest` responding, the number check, Docker.

## Demo (guide §9.1) — four things to show

1. A normal answer: "Why did the bill go up?" → the five-cause bridge.
2. A case where it cannot decide: "What will the tariff be next year?" → `not_in_data`.
3. A trap handled: the mix shift raises kWh per unit, and the system blames mix, not a machine.
4. A what-if: "What if the tariff had been 10% lower?"

Everyone must be able to explain, without notes: why Shapley and not sequential attribution; why waste is the residual; why the baseline is fitted on the reference quarter only; how the number registry stops an invented figure; why there are two LLM calls and no agent loop.

---

# Part 2 — Individual responsibilities

Tasks are in the order to do them. **P0** must ship. **P1** should ship. **P2** only if P0 and P1 are merged.

## Owner — orchestration, API, packaging, integration

**Files:** `rng/graph/state.py`, `registry.py`, `llm.py`, `brief.py`, `nodes.py`, `graph.py` · `rng/api/main.py`, `routes.py` · `Dockerfile`, `docker-compose.yml` · `README.md` · `docs/traces/` · `tests/test_registry.py`, `tests/test_api.py`

| # | Pri | Task | Done when |
|---|---|---|---|
| 1 | P0 | `registry.py`: `put(key, value, unit, src, ev)`, rows for the writer, and `check(prose)` that extracts every numeral and matches it within ±0.05 | A brief with one invented number fails the check in a test |
| 2 | P0 | `llm.py`: `MOCK_LLM` keyword router returning `{answer_type, period, compare_to, asset}`, and a slot-filled template writer | Six question types route correctly offline |
| 3 | P0 | `state.py`, `nodes.py`, `graph.py`: the seven nodes — classify, guard, load_analysis, handler, write_brief, verify, respond | `answer(question, period, compare_to)` returns an `AnalyzeResponse` |
| 4 | P0 | `api/main.py`, `routes.py`: the four endpoints. Startup calls B's `precompute()`. `/ingest` is a thin wrapper over B's loader | `curl` works for all four |
| 5 | P0 | Trace: write `runs`, `run_steps`, `registry_values`; `/trace/{run_id}` returns the Annex C shape | Three traces saved in `docs/traces/` for three answer types |
| 6 | P0 | Real Gemini writer behind `LLM_PROVIDER` / `LLM_MODEL`, with one regeneration then template fallback | Works with a key; falls back cleanly without one |
| 7 | P0 | `Dockerfile` (`python:3.12-slim`) and `docker-compose.yml` starting API and UI | Clean clone, `docker compose up`, `/health` is ok |
| 8 | P1 | `README.md`: architecture diagram, setup, sample `curl`, assumptions, limitations, edge cases | Matches the deliverables list |
| 9 | P1 | Real Gemini classifier, with the keyword router as fallback | |

**Handler routing** (the guard node picks `clarification_needed` when the period is missing or not in the data, and `refused` for requests outside the data or rules):

| Question | `answer_type` | Reads cache `kind` |
|---|---|---|
| Why did the bill go up? | `decomposition` | `bridge` (+ `findings`) |
| Which assets use the most? | `calculated` | `asset_totals` |
| Energy used when nothing is produced? | `calculated` | `idle_load` |
| What should we do first? / Which pays back fastest? | `recommendation` | `recommendations` |
| What if the tariff had been 10% lower? | `calculated` | C's `what_if_tariff` via `pipeline.current()` |
| What will the tariff be next year? | `not_in_data` | — |

**Also yours:** merging PRs, guarding the frozen files, the `final` tag, collecting each person's `docs/statements/<name>.md` into the contribution statement and AI-usage disclosure.

## A — validation, savings, evaluation, UI

**Files:** `rng/data/validate.py` · `rng/analysis/savings.py` · `rng/eval/cases.py`, `run_eval.py` · `rng/ui/app.py` · `docs/datacard.md`, `docs/validation_output.txt`, `docs/eval_report.md` · `tests/test_validate.py`, `tests/test_savings.py`

| # | Pri | Task | Done when |
|---|---|---|---|
| 1 | P0 | `validate.py`: the six §4.5 checks on the raw CSVs — sub-meters within tolerance of main, negatives and resets, regular single-time-zone timestamps, tariff windows contiguous and covering, production days against shift calendar, row counts. Runnable as `python -m rng.data.validate data/primary` | Output saved to `docs/validation_output.txt`; it reports the reset, the gaps and the mislabelled holiday |
| 2 | P0 | `savings.py`: `estimate_savings(findings)`. kWh, money and CO2e as low/high from `finding.kwh_per_year` × `recovery_fraction`, an average price, and the emission factor; payback from `fix_cost`; rank by payback then ease. All assumptions from `rng.config.assumptions()` | Test with a hand-made `Finding`: every range ordered, every recommendation has an `evidence_ref`, ranking correct |
| 3 | P0 | `eval/cases.py`: at least 20 cases — ≥3 where the answer is price or mix, ≥3 numeric, ≥2 unanswerable, ≥2 what-if, ≥2 attempts to make the LLM invent a number | Each case has a question, periods, expected `answer_type` and a check |
| 4 | P0 | `ui/app.py`: question box, period inputs, summary, bridge waterfall chart (R11), recommendations table, assumptions, trace link. Calls the API over HTTP only | One chart explains the 15% to a manager |
| 5 | P0 | `eval/run_eval.py`: posts each case to the API, compares against `ground_truth.json`, writes `docs/eval_report.md` with decomposition error per cause, reconciliation rate, waste detection, savings accuracy, number faithfulness, latency p50/p95, LLM calls and tokens | Report exists for `data/primary`; run `data/holdout` too if time allows |
| 6 | P1 | `docs/datacard.md` (Annex D), filled from `ground_truth.json` and the validation output | All seven fields present |

**You need from others:** the API (Owner) for tasks 4 and 5 — build both against the frozen `AnalyzeResponse` schema until it is up. Nothing for tasks 1–3.
**Note:** `savings.py` sits behind the import wall. It must not read the leak size from the generator; it only sees the `Finding` it is given.

## B — the data path

**Files:** `rng/ingest/tariff.py`, `loader.py`, `clean.py`, `pipeline.py` · `rng/store/cache.py` · `rng/analysis/assets.py` · `scripts/load.py` · `tests/test_tariff.py`, `tests/test_ingest.py`

| # | Pri | Task | Done when |
|---|---|---|---|
| 1 | P0 | `tariff.py`: `resolve_prices`. Use `peak_start_hour` / `peak_end_hour` if present, else the defaults in `rng.config`; handle `flat`; join `valid_from <= ts < valid_to` and accept an inclusive `23:59:59` end; raise on an uncovered timestamp. **C is waiting on this — do it first** | Tests: our file, a file without hour columns, a flat tariff, an uncovered timestamp |
| 2 | P0 | `loader.py`: `load_csv`, `load_dir`. Extra and reordered columns accepted, missing required column rejected, bad rows counted | Loads `data/primary` and `data/holdout`; returns `rows_loaded` and `rows_rejected` |
| 3 | P0 | `clean.py`: regular 15-minute grid per meter, impute gaps (`imputed=True`), fix the negative reset (`reset_adj=True`), drop duplicates, report the reconciliation gap, flag suspected holidays. Never rescale meters to force agreement | `CleaningReport` lists 3 gaps and 1 reset on `data/primary` |
| 4 | P0 | `cache.py`: `put` / `get` with `INSERT OR REPLACE`; `None` ↔ `''` for `compare_to` | Round-trips a payload |
| 5 | P0 | `pipeline.py`: `precompute(data_dir)` — load, clean, write `meter_clean` and `datasets`, then run every tier and cache it; `current()` returns clean, model, dataset_id. **Wrap each tier in its own `try/except`** so a broken bridge does not stop asset totals from being cached. `scripts/load.py` is the CLI over the same function | `python scripts/load.py data/primary` fills the cache with whatever tiers are implemented |
| 6 | P0 | `assets.py`: `asset_totals` — groupby and sort on measured data, share of main, cost via `resolve_prices` | Ranked list for any period in the data |

**You need from others:** nothing to start. Task 5 calls C's and A's functions through their stub signatures; they fill in as they merge.
**Others need from you:** C needs task 1 by 14:55 and `CleanTables` (tasks 2–3) by 15:10. The Owner needs task 5 by 15:25.

## C — the analytics core

**Files:** `rng/analysis/baseline.py`, `decompose.py`, `waste.py`, `whatif.py` · `tests/test_analysis.py`

| # | Pri | Task | Done when |
|---|---|---|---|
| 1 | P0 | `baseline.py`: daily main-meter kWh on units per product, CDD and HDD. **Fit on the reference period only.** Report R² | R² reported; a test shows that fitting on both periods shrinks waste |
| 2 | P0 | `decompose.py`: `C(S)` for all 16 subsets, Shapley over tariff, volume, mix and weather, waste = actual cost − `C(all)`. **Price interval by interval**: spread predicted daily kWh over the 15-minute grid with the reference load shape, then multiply by `resolve_prices`. Actual costs come from the meter, never the model. Call `bridge.assert_adds_up()` | The five causes sum to the measured change exactly on both datasets |
| 3 | P0 | `waste.py`: `find_idle_load` (zero-production intervals; floor, hours, night share, cost per asset) and `detect_leaks`. If `bridge.waste_pp` is near zero, return nothing. Skip weather-driven assets when the weather term explains their night load | Finds one asset on `data/primary`; finds nothing when the leak is absent |
| 4 | P0 | Ranges: refit the baseline on resamples (start with 15), recompute the split, fill `ranges_pp` with the 10th and 90th percentiles | Every cause has a low and a high |
| 5 | P1 | `whatif.py`: `what_if_tariff(clean, period, compare_to, model, scale)` — scale current-period prices, re-run `decompose`, return both bridges and the per-cause difference | "10% lower tariff" returns a changed tariff term and an unchanged waste term |
| 6 | P2 | Sequential (Laspeyres) attribution alongside Shapley, for the two-configuration comparison | |

**Tests to write** (`CLAUDE.md` §9): baseline fitted on both quarters loses the waste; daily-total pricing gives a different tariff term from interval pricing; waste equals the residual.
**You need from others:** `resolve_prices` and `CleanTables` from B. Until they merge, read `data/primary/*.csv` with a throwaway helper inside your test file — do not add a second cleaning path under `rng/`.
**Check your work against** `data/primary/ground_truth.json` by eye or in `rng/eval` only. Your code must never read it. Expect a small gap on weather and waste: the generator's cooling response is deliberately non-linear.

---

## Every person, before 16:00

Write `docs/statements/<yourname>.md` with three short parts: what you built, which parts an AI assistant generated and how you verified them, and a signed line declaring the work original. One file each, so nobody collides.
