# Factory Energy Cost Decomposition and Savings Agent

A factory's electricity bill is 15% higher than last quarter. This system says
why: it splits the change into **tariff, production volume, product mix, weather
and controllable waste** — five numbers that add up to the total — and only then
points at waste, with costed actions, CO2e and payback, each as a range.

**The one rule:** the LLM classifies the request and writes the prose. It never
computes a number and never decides what is wasteful. Every figure in a
response comes from code and is checked back against a registry before it
leaves.

HCLTech Future Ready AI Engineer Hackathon, IIIT Delhi, 11 October 2026.

## Run it

```bash
git clone https://github.com/silverbolt08/RNG.git && cd RNG
docker compose up --build
```

- API: http://localhost:8000 (interactive docs at `/docs`)
- UI: http://localhost:8501

This works from a clean clone with no configuration: `MOCK_LLM` defaults to
`true` and the whole system runs offline. To use Gemini, copy `.env.example` to
`.env`, set `GEMINI_API_KEY` and `MOCK_LLM=false`, and run the same command.

Without Docker (Python 3.11 or 3.12):

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn rng.api.main:app --port 8000          # loads data and precomputes at startup
streamlit run rng/ui/app.py                   # in a second terminal
```

## Try it

```bash
curl -s localhost:8000/health

curl -s -X POST localhost:8000/analyze -H "Content-Type: application/json" \
  -d '{"question": "Why did the bill go up 15%?", "period": "2026-Q3", "compare_to": "2026-Q2"}'

curl -s -X POST localhost:8000/analyze -H "Content-Type: application/json" \
  -d '{"question": "Which assets use the most energy?"}'

curl -s -X POST localhost:8000/analyze -H "Content-Type: application/json" \
  -d '{"question": "What will the tariff be next year?"}'        # -> not_in_data

curl -s localhost:8000/trace/<run_id>                            # full audit record

curl -s -X POST localhost:8000/ingest -F "file=@data/primary/tariff.csv" -F "table=tariff"
```

| Endpoint | Purpose |
|---|---|
| `POST /analyze` | `question`, optional `period` and `compare_to`. Returns `run_id`, `answer_type`, `summary`, `bridge[]`, `recommendations[]`, `assumptions[]`, `limitations[]` |
| `POST /ingest` | Multipart CSV plus the table name (`meter_15min`, `tariff`, `production`, `weather`). Returns `rows_loaded`, `rows_rejected`, `status`, then re-runs the analysis |
| `GET /health` | Status of the API, the data store and the LLM |
| `GET /trace/{run_id}` | Every step, tool, input and output of one run, plus its number registry |

Command line, same code paths:

```bash
python scripts/load.py data/primary           # load, clean, precompute (the CLI loader)
python -m rng.data.validate data/primary      # the validation checks of guide section 4.5
python -m rng.eval.run_eval                   # the evaluation set and report
python -m rng.data.generator --profile primary --out data/primary   # regenerate the data
python -m pytest && python -m ruff check rng tests
```

## Architecture

```
STARTUP   CSV files -> load -> validate -> clean -> PRECOMPUTE -> DuckDB (analysis_cache)

REQUEST   Streamlit --HTTP--> FastAPI --> LangGraph
                                           1 classify        LLM call 1
                                           2 guard           in scope? period in the data?
                                           3 load_analysis   cache read
                                           4 analyse         deterministic handler
                                           5 write_brief     LLM call 2
                                           6 verify          every numeral vs the registry
                                           7 respond         + persist the trace
```

A fixed graph, not an agent loop. There are a known number of question types,
so the LLM picks one and deterministic code does the rest. That bounds latency
and the call count (at most three: classify, write, one regeneration) and makes
the trace readable top to bottom.

| Decision | Choice | Why |
|---|---|---|
| Agents | Two LLM roles (classifier, writer); six deterministic handlers | Every analytical step is a lookup or a sort. An LLM around a sort is a slower `if` that can hallucinate |
| Compute | Everything precomputed at startup | The data is static, so every answer is a cache lookup |
| Store | DuckDB, one file | Analytical queries over ~158k meter rows |
| UI | Streamlit calls the API over HTTP | The demo exercises the path the judges test |
| Periods | Read from the data, never assumed | No quarter label or meter name is hard-coded anywhere |

### How a number is kept honest

Before the writer runs, every figure is written to a **registry** with its
unit, the tool that produced it and an evidence reference. The writer LLM is
given that table and nothing else — no database, no dataframes. After it
writes, every numeral in the prose is extracted and matched back to the
registry (a numeral written to *d* decimals matches a value that rounds to it).
A draft that fails is regenerated once with the offending numbers named, then
replaced by a slot-filled template. Worst case is stilted prose; a number that
no tool produced cannot leave the `verify` node. The registry is stored per run
and returned by `/trace`.

Requests to guess or invent a figure are refused by a deterministic check that
runs before the model, so a prompt cannot talk its way past it.

## Method

**Baseline.** `kWh/day ~ b0 + sum(beta_p * units_p) + beta_cdd * CDD + beta_hdd * HDD`,
one coefficient per product family, fitted on the **reference quarter only**.
Fitting on both quarters would teach the model that the leak is normal.

**Decomposition.** `C(S)` is the bill if the causes in `S` sit at current-quarter
levels and the rest at the reference quarter: 16 evaluations for four causes.
Tariff, volume, mix and weather are their **Shapley values**, so no cause is
favoured by the order it is processed in (sequential attribution gives 24
different answers). **Waste is the residual**: the actual bill, from real meter
readings at the real tariff, minus `C(all)`. The five numbers therefore add up
by construction. The bill is priced **interval by interval**, never as a daily
total against an average price.

**Order of attribution.** `detect_leaks(clean, bridge, idle)` takes the finished
bridge as a required argument, so a machine cannot be examined before tariff,
volume, mix and weather have been removed. If the residual is near zero, it
finds nothing.

**Ranges.** The baseline is refitted on resamples and the split recomputed;
the 10th and 90th percentiles are reported. Savings ranges also carry the
stated assumption ranges. No range is invented.

## Data

The team generates its own data to the Annex B schema
(`rng/data/generator.py`). It simulates the plant from physical drivers and the
analysis fits from the CSV files alone: a test forbids the analysis code from
importing the generator or reading its answer key. `data/primary` rises 15.00%;
`data/holdout` (22%, different leak, mix moving the other way) is kept for
evaluation. See `docs/datacard.md`.

## Assumptions

Declared in every response's `assumptions[]`, and held in `rng/config.py`:

- Emission factor 0.71 kg CO2e per kWh (stated in the data pack).
- Peak tariff hours 09:00-21:00 UTC on weekdays. Annex B's tariff table names the period but not the hours; a file without `peak_start_hour` / `peak_end_hour` columns gets this default.
- All clock hours are UTC, the time zone of the meter timestamps.
- Recovery fraction 60-85% of identified waste; fix costs per action. Neither is in any input file.
- Sub-meters reconcile to the main meter within 6%.

## Limitations

- The baseline is linear in degree-days. A non-linear cooling response lands in the waste residual, so waste is an upper bound on what is controllable.
- Waste is "unexplained by the model", not a direct measurement.
- One reference quarter of data limits how well weather effects are estimated.
- The number check matches values, not meanings: a correct figure attached to the wrong label would pass.
- A what-if rescales prices only; it assumes consumption does not respond to price.

## Edge cases handled on purpose

| In the data | What the system does |
|---|---|
| Sub-meters do not sum to the main meter | Reports the gap against the tolerance; never rescales a meter to hide it |
| Missing intervals, a meter reset | Regular 15-minute grid, gaps imputed and flagged, the negative reading corrected and flagged |
| Tariff change mid-quarter | Price joined per interval from the validity windows |
| Mix shift raising kWh per unit with no waste | The mix term absorbs it; no machine is blamed |
| Hot spell raising cooling load | The weather term absorbs it; cooling assets are not flagged for it |
| Holiday logged as a normal shift | Flagged when production is near zero on a claimed working day |

## Deliberately not built

- **Vector store / retrieval** — there is no document corpus.
- **Multi-agent orchestration** — every analytical step is deterministic.
- **ML beyond a linear baseline** — it must be explainable and invertible for counterfactuals.
- **Cloud tracing** — tracing is local, in DuckDB.

## Repository

```
rng/api        FastAPI app, routes, the frozen schemas
rng/graph      LangGraph nodes, handlers, number registry, LLM calls, template writer
rng/ingest     loader, tariff pricing, cleaning, startup precompute
rng/analysis   baseline, decomposition, waste, savings, what-if, asset totals
rng/store      DuckDB schema, connection, cache
rng/data       generator, validation
rng/eval       evaluation cases and runner
rng/ui         Streamlit app
data/          primary and holdout datasets with ground truth
docs/          approach, data card, validation output, evaluation report, sample traces
tests/         pytest suite
```

Team ownership and the build plan are in `WORKPLAN.md`; design decisions and
their reasons are in `CLAUDE.md`. Work happens on feature branches merged to
`main` by pull request.
