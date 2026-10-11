# CLAUDE.md

Context for this repo. Everything below is **already decided** — do not re-litigate
these choices unless the user asks. Where a decision has a reason recorded, the
reason is the thing to preserve.

---

## 1. Project

**HCLTech Future Ready AI Engineer Hackathon**, IIIT Delhi, **11 October 2026**.
One-day build, code freeze **16:15**, demos from 16:30.

**Track chosen: Factory Energy Cost Decomposition and Savings Agent.**
(Three other tracks existed — Multi-Plant Allocation, Production Re-Planner,
Wind Fleet. Energy was chosen; the others are not relevant.)

**The problem.** A factory's electricity bill is 15% higher than last quarter.
Build a system that splits that 15% into tariff, volume, product mix, weather and
controllable waste — which must add to the total — then finds real waste and
recommends actions with costed savings, CO2e and payback.

**Team of 4.** Repo owner (git identity `silverbolt08`) owns the orchestration and
evaluation layer. Three teammates own data generation, ingest/cleaning, and
analytics respectively — see §7.

Repo: `github.com/silverbolt08/RNG`. Workflow: feature branches + PRs, documented
in `README.md`.

---

## 2. The one rule everything else serves

**The LLM classifies the request and writes the prose. It never computes a number
and never decides what is wasteful.**

"Plans, numbers or rankings produced only by LLM text, with no tool, solver or
model behind them" is a **disqualification condition**, not a deduction. The
architecture is shaped so that this is structurally impossible rather than merely
discouraged.

Other disqualification conditions: answers hard-coded for demo or test cases; no
evaluation of any kind; real company data; code shared between teams.

---

## 3. Hard constraints (mandated by the organisers)

| Layer | Required |
|---|---|
| UI | Streamlit (scored on usability only — keep minimal) |
| API | FastAPI + Uvicorn + Pydantic v2 |
| Orchestration | LangGraph (Python) |
| Store | SQLite / Parquet / DuckDB — **we use DuckDB** |
| Analytics | pandas, numpy, scikit-learn or statsmodels, matplotlib/plotly |
| LLM | Config switch `LLM_PROVIDER` / `LLM_MODEL`. **Gemini, default `gemini-3.5-flash-lite`**, upgrade path `gemini-3.8-flash`. Key in `.env`, never committed |
| Packaging | Docker + `docker compose up` must work from a clean clone |
| Python | Pinned 3.11 or 3.12, pinned requirements, pytest, ruff |
| Tracing | **Local only** — no cloud tracing tools permitted |

**API contract is fixed** (judges run identical tests against every team):

- `POST /analyze` — body: `question` (text), optional `period` (e.g. `2026-Q3`),
  optional `compare_to` (e.g. `2026-Q2`)
- `POST /ingest` — multipart CSV + JSON naming the table
  (`meter_15min` / `tariff` / `production` / `weather`).
  Returns `rows_loaded`, `rows_rejected`, `status`.
  **Thin wrapper over the CLI loader — no separate machinery.** We run on a
  static generated dataset, but the endpoint stays because it is in the fixed
  contract and the deliverables list, and a 404 during live testing is a visible
  gap. It reloads and re-precomputes; that is all
- `GET /health` — status of API, data store, LLM
- `GET /trace/{run_id}` — full record of steps, tools and data used
- Plus a CLI loader for the same schemas

Response shape for `/analyze` is specified in the guide §6.1 — `run_id`,
`answer_type`, `summary`, `bridge[]`, `recommendations[]`, `assumptions[]`,
`limitations[]`. Every recommendation carries `action`, `rationale`,
`evidence_refs`, `confidence`, `estimated_impact`, `requires_approval`.

`answer_type` is one of: `decomposition`, `recommendation`, `calculated`,
`not_in_data`, `clarification_needed`, `refused`.

**Annex B data schema is fixed** — extra columns allowed on input, listed ones
must exist:

```
meter_15min.csv   meter_id, ts(UTC), kwh
tariff.csv        valid_from, valid_to, period(peak|off_peak|flat), price_per_kwh
production.csv    date, product, units, shift_pattern, is_holiday
weather.csv       date, avg_temp_c, cooling_degree_days, heating_degree_days
```

---

## 4. Architecture

Two LLM calls per request, **both at the edges**. Everything between them is
deterministic code.

**The data is static** — the one dataset we generate. It is loaded and fully
analysed once at startup; the running system only answers questions.

```
STARTUP   scripts/load.py ──► loader → validate → clean → PRECOMPUTE
                                                              │
                                                              ▼
Streamlit ──HTTP──► FastAPI ──┬── /analyze ──► LangGraph:     DuckDB
                              │     1 classify        LLM call 1
                              │     2 guard           (period exists? in scope?)
                              │     3 load_analysis   (cache read)
                              │     4 pipeline        deterministic handler
                              │     5 write_brief     LLM call 2
                              │     6 verify          numbers vs registry
                              │     7 respond         + persist trace
                              │
                              └── /ingest ──► same loader, re-runs PRECOMPUTE
```

### Decisions and why

| Decision | Choice | Why |
|---|---|---|
| Routing | LLM classifies intent to JSON `{answer_type, period, compare_to, asset}`, then deterministic dispatch | Six known question types. An agentic tool-picking loop adds variable latency and unbounded call count with no accuracy gain, and makes traces unreadable |
| Agent count | **Two** LLM agents (classifier, writer) + six **deterministic** handlers | The organisers' own Annex C reference trace shows two agents (`analyst`, `writer`) and `llm_calls: 1`. Specialisation belongs in the `tool` field, not the `agent` field. Every handler is a lookup/sort — an LLM around a sort is a slower `if` branch that can hallucinate |
| Number safety | Registry + post-hoc validation + retry + template fallback | See §6 |
| Attribution order | Enforced by **function signature**, not prompt | `detect_leaks(clean, bridge, idle)` cannot be called before the bridge exists |
| Store | DuckDB, single file | Analytical over ~158k meter rows, reads Parquet; guide warns against putting time series in SQLite |
| Compute timing | Precompute **at startup**, serve from cache | Data is static, so everything is warm before the first request. Latency is a reported metric and this makes every answer a lookup |
| Data lifecycle | One static generated dataset. No versioning, no cache invalidation | Nothing changes at runtime. `dataset_id` stays as a column (cheap, keeps the cache key honest) but there is no invalidation logic to get wrong |
| UI coupling | Streamlit calls FastAPI over **HTTP**, does not import modules | Demo exercises the exact path judges test; one code path |
| Periods | Fully period-agnostic | Never assume specific quarter labels. The eval set spans many generated scenarios, and hard-coding `2026-Q3` is a disqualification condition |
| Vector store / RAG | **Not built** | No document corpus. Guide: "no marks for using it." Pure cost |

### MOCK_LLM

`MOCK_LLM=true` swaps the classifier for the keyword router and the writer for the
slot-filled template. The whole system runs offline. Required for cheap eval runs
and for when the venue network dies.

---

## 5. Analytical design

### Tiered, not monolithic

Different questions need wildly different machinery. **Do not route everything
through the decomposition pipeline.** Four independently cached results:

```
meter_clean ──┬──► asset_totals        cheap, standalone, key (dataset_id, period)
              ├──► idle_load           cheap, + shift calendar, key (dataset_id, period)
              └──► baseline fit ──► 16 counterfactuals ──► bridge
                                                            │   key (dataset_id, period, compare_to)
                                            leak findings ◄─┘   requires bridge
                                                  │
                                              savings ──► ranking
```

Note the **cache keys differ**: top-users and idle-load need one period; the
bridge needs a pair. A single blob keyed by `(period, compare_to)` would break
"which assets used most in Q3?" by forcing an arbitrary comparison period the
question never asked for.

Rationale: top-users is a `groupby` and a sort. Running it through a fitted model
would be both wasteful and *wrong* — it would report predictions where the
question asks for measurements. Also gives demo insurance: if the decomposition
is broken at 15:00, three of six question types still answer correctly.

### Baseline (R2)

```
kwh_day ~ b0 + Σ_p (beta_p · units_p) + beta_cdd·CDD + beta_hdd·HDD
```

One coefficient per product family, so `beta_p` reads as "kWh per unit of product
p" — explainable in Q&A. **Fit on the reference quarter only** (see §9, failure 1).
Report R² in the trace.

### Decomposition (R3) — Shapley, waste as residual

`C(S)` = the bill if the causes in set `S` sit at current-quarter levels and the
rest stay at the reference quarter. Four causes → **16 memoised evaluations**.

Shapley averages each cause's marginal contribution over all orderings, so no
cause is advantaged by processing position. Sequential/Laspeyres attribution gives
a different answer depending on arbitrary order — with four causes there are 24
orderings.

```
tariff, volume, mix, weather  =  Shapley values over C   (sums to C(all) − C(none))
waste                         =  actual_cost − C(all)    (the residual)
sum of all five               =  actual_Q3 − actual_Q2   exactly, by construction
```

`actual_cost` comes from **real meter readings priced at the real tariff**, never
from the model. Additivity is therefore structural; the `assert` in the code is a
regression guard, not a reconciliation mechanism.

### Ranges (R8, Annex A)

Bootstrap: refit the baseline on 50 resamples, recompute the Shapley split on
each, report 10th/90th percentiles. Savings ranges propagate that plus stated
assumption ranges (`recovery_fraction` 0.60–0.85, `fix_cost`). **No range is
invented.** Fix costs are not in the data — they are our assumption, declared in
every response's `assumptions[]` and held in a config file, not scattered in code.

### Idle load (R6) and leaks (R7)

Idle: filter meter data to zero-production intervals (nights, weekends, genuine
holidays), sum and take the median floor per asset.

Leaks: per asset, `night_share = median(night kWh) / median(day kWh)`; plus step-
change detection to date the fault. Two rules it must obey:

- Do not flag HVAC for high night load when the weather term already explains it
- **If `waste_pp` is near zero, find nothing.** The mix-shift trap exists
  specifically to test this, and refusing to blame a machine there is worth points

---

## 6. The number registry — the faithfulness mechanism

Before the writer LLM runs, every figure is written to a registry row:

```python
reg.put("tariff_pp", 8.1, "pp", src="decompose_change", ev="tariff:2026-Q3")
```

label · value · unit · producing tool · evidence reference.

One table, three jobs:

1. **It is the only thing the writer LLM sees.** No DB connection, no dataframes.
   It cannot look a number up, only use what it was handed
2. **It is the validation checklist.** After generation, extract every numeral
   from the prose and match against the registry with rounding tolerance
   (±0.05 — the model writes "8.1" for 8.0999; string equality will fight you all
   day). Unmatched → regenerate once → slot-filled template fallback
3. **It is the evidence trail.** `ev=` populates `evidence_refs` in the response
   and appears in `/trace`

Worst case is stilted prose. A wrong number cannot leave the `verify` node.

---

## 7. Repo layout and ownership

One owner per directory — these are the merge-conflict boundaries.

```
rng/
  api/        main.py  routes.py  schemas.py          ← repo owner
  graph/      nodes.py  state.py  registry.py  brief.py  ← repo owner
  data/       generator.py  validate.py  datacard.md   ← teammate A
  ingest/     loader.py  clean.py                      ← teammate B
  analysis/   baseline.py  decompose.py  waste.py  savings.py  ← teammate C
  store/      db.py  schema.sql  cache.py              ← teammate B (shared)
  ui/         app.py                                   ← teammate A (after generator)
  eval/       cases.py  run_eval.py                    ← repo owner
tests/  scripts/  docs/  docker-compose.yml  .env.example
```

**Only two files are shared: `store/schema.sql` and `api/schemas.py`. Write them
first and freeze them.**

### DuckDB tables

Raw (exactly Annex B): `meter_15min`, `tariff`, `production`, `weather`.

System:

| Table | Purpose |
|---|---|
| `datasets(dataset_id, loaded_at, source, row_counts)` | one row in normal operation; keeps the cache key honest and records row counts for the data card |
| `meter_clean(meter_id, ts, kwh, imputed, reset_adj)` | flags preserved for the demo |
| `analysis_cache(dataset_id, period, compare_to, payload, computed_at)` | note: `compare_to` nullable for single-period results |
| `runs(run_id, question, answer_type, request, response, llm_calls, tokens, latency_ms)` | R12 |
| `run_steps(run_id, seq, agent, tool, input, output, ms)` | Annex C trace shape |
| `registry_values(run_id, key, value, unit, source_tool, evidence_ref)` | §6 |

### Module contracts

```python
clean(raw) -> (CleanTables, CleaningReport)
fit_baseline(period) -> BaselineModel          # .predict(units, cdd, hdd), .r2
decompose(period, compare_to, model) -> Bridge # sums to total
find_idle_load(clean) -> dict[asset, IdleStats]
detect_leaks(clean, bridge, idle) -> list[Finding]   # bridge required = guardrail
estimate_savings(findings) -> list[Recommendation]
```

### What-if (replaces the late-data-drop path)

With static data there is no late drop to diff against, so **the what-if is now
the only way to satisfy §9.1's "one re-plan or what-if" demo requirement.** It is
not optional — build it.

A what-if takes a modified input, re-runs the counterfactuals, and diffs the new
bridge against the base one, cause by cause:

```
"What if the tariff had been 10% lower?"   -> scale the tariff table, recompute C(S),
                                              diff against the cached bridge
"What if we fix the compressor leak?"      -> recompute savings with the finding resolved
```

Fully deterministic — no LLM, no new data, no ingest. The eval set needs ≥2
what-if cases anyway (§10), so this earns twice.

---

## 8. Data generation

**The team generates its own data** (confirmed with the panel — there is no
HCLTech data pack and no hidden answer key). Generate to the Annex B schema
exactly.

The dataset is **static**: generated once, loaded at startup, never changes while
running. But **hardcode nothing** to our generator's quirks — the loader must
tolerate extra or reordered columns, and nothing downstream may assume specific
meter names, a specific number of sub-meters, or specific quarter labels. Answers
hard-coded for the demo dataset are a disqualification condition, and a judge may
still try `/ingest`.

Minimums: ≥6 sub-meters plus main, ≥2 quarters, one tariff change part-way
through, ≥1 hidden waste pattern. Tune the generator so the total rise lands near
**15%** — the whole problem narrative depends on it. Make the target a parameter
so the eval set can span 8% / 15% / 22%.

### Anti-circularity (important)

If the generator and the analyser are mirror images, a perfect result proves
nothing, and it edges toward the "hard-coded for test cases" disqualification.

- **Generate from physical drivers**: furnace from units × per-product energy
  intensity, HVAC from cooling degree-days, compressors from shift hours plus a
  leak starting on a known date
- **The analyser fits from the CSVs alone** and never imports a generator constant
- **Deliberately give the generator something the linear baseline cannot
  represent** (piecewise temperature response). A small residual error is then
  expected and honest rather than suppressed

Generator is seeded and emits `ground_truth.json` with the true split and the
location of every injected anomaly. Generate a **second holdout set** with a
different seed and different patterns; do not run against it until the eval pass.

### Six edge cases, injected deliberately (guide §4.4)

| Edge case | Inject | Detect |
|---|---|---|
| Sub-meters don't sum to main | unmetered base load + per-meter noise | reconciliation vs stated tolerance |
| Missing intervals, meter resets | drop blocks; cumulative counter rollover | gap report; reset = negative delta |
| Tariff change mid-quarter | two tariff rows straddling the boundary | price joined per interval |
| Mix shift raising kWh/unit, no waste | shift weights to energy-intense product | mix term absorbs it, waste stays flat |
| Hot period raising cooling | temperature excursion driving CDD | weather term; HVAC excluded from leak search |
| Holiday logged as normal shift | `is_holiday` false on a near-zero day | shift calendar vs production cross-check |

Each injected case doubles as an eval case. **The mix-shift trap is the demo
case** — it is where the correct answer blames product mix, not a machine.

Annex D data card is mandatory: name/version, how it was made (prompts, seeds,
script), contents, traps, checks run, known limits, privacy confirmation.

---

## 9. Four ways this breaks

Write a test for each.

1. **Fitting the baseline on both quarters** — the leak becomes "normal" and waste
   collapses to zero
2. **Pricing daily totals against a time-of-day tariff** — tariff is the largest
   bridge term; it must be priced interval by interval, spreading predicted daily
   kWh across the 15-minute grid using the reference load shape
3. **Estimating waste separately instead of taking the residual** — the five
   numbers stop genuinely adding up and additivity becomes a fudge
4. **Running leak detection before the bridge** — straight into the "blamed a
   machine first" deduction (10 points)

---

## 10. Evaluation

Because we generate the data we know the true split, so decomposition error is
**measured, not estimated**. ≥20 cases, built from generator ground truth:
≥3 where the answer blames price or mix not machines, ≥3 numeric checks,
≥2 unanswerable, ≥2 what-ifs, ≥2 attempts to make the LLM invent a number.

Report: decomposition error (MAE per cause, pp) · total reconciliation pass rate ·
waste detection (leak found, no false positives on traps) · savings accuracy
(true value inside range) · number faithfulness (unaided pass rate, regeneration
and fallback counts) · latency p50/p95 · LLM calls and tokens.

**For the §7 higher marks**: run Shapley *and* sequential Laspeyres across 50
generated scenarios and report MAE per cause for each. Justify the final choice
with numbers, not assertion. `MOCK_LLM` makes this free to run repeatedly.

---

## 11. Deliberately not built

State these in the demo — the brief rewards them explicitly.

- **Vector store / RAG** — no document corpus; guide awards no marks
- **Multi-agent orchestration** — every analytical step is deterministic
- **ML beyond a linear baseline** — must be explainable in Q&A and invertible for
  counterfactuals; report the R² achieved and where it limits us
- **Cloud tracing** — not permitted

---

## 12. Deliverables checklist

- Git repo **tagged `final`**, README with architecture diagram, setup/run steps,
  sample curl commands, assumptions, limitations, known edge cases
- `docker compose up` works **from a clean clone** (test this at 16:00, not 16:14)
- Validation script + its output; CLI loader
- Models/solver code with tests on key functions
- Eval set + report
- Data card (Annex D) with prompts and generator script
- **Three sample trace records** for different answer types
- AI-usage disclosure
- Team contribution statement
- Signed declaration of original work from every member

Judging: 10 min demo · 10 min judges live-testing with unseen data · 10 min Q&A
**directed at individuals, including about parts they did not build**. Everyone
must be able to explain the whole system. Everyone commits — git history feeds the
individual hiring decision.

---

## 13. Status (as of this file being written)

Decided: track, architecture, analytical method, guardrails, repo layout,
ownership, evaluation plan. Approach document presented to mentors —
`docs/approach.pdf`, rebuild with `python docs/build_approach_pdf.py`.

Built: repo, README with team workflow, `.gitignore`, the approach PDF.
**No application code yet.**

Next, in this order:

1. `api/schemas.py` (Pydantic, mirroring guide §6.1) and `store/schema.sql` —
   these block everyone else
2. Crude but schema-correct generator, committed fast — three people are blocked
   on it. A rough generator at 11:00 beats an elegant one at 13:00
3. Stubs for every module contract in §7, so the four workstreams integrate early
4. One question end to end before 11:30, even if the answer is wrong

Confirmed with the panel: we generate our own data; no hidden metric; Annex B
schema still applies; 15% is still the target; nothing is hard-coded.

**The dataset is static** — generated once, loaded at startup. No ingest
machinery, no versioning, no cache invalidation. `/ingest` survives only as a
thin wrapper over the loader, and the what-if path (§7) replaces the
late-data-drop diff as the demo's re-plan item.
