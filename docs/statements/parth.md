# Contribution statement: Parth (role A)

## What I built

Validation, savings, evaluation and the user interface.

| Area | Files |
|---|---|
| Data validation (guide 4.5) | `rng/data/validate.py`, `docs/validation_output.txt`, `docs/validation_output_holdout.txt` |
| Savings and ranking (R8, R9) | `rng/analysis/savings.py` |
| Evaluation set and runner (guide 10) | `rng/eval/cases.py`, `rng/eval/run_eval.py`, `docs/eval_report.md`, `docs/eval_report_holdout.md` |
| User interface (R11) | `rng/ui/app.py` |
| Data card (Annex D) | `docs/datacard.md` |
| Tests | `tests/test_validate.py`, `tests/test_savings.py`, `tests/test_eval.py`, `tests/test_ui.py` |

- **Validation:** six checks on the raw CSVs. On both datasets it finds the
  meter reset, every missing-interval gap and the holiday logged as a working
  day, without reading the answer key.
- **Savings:** turns each finding into a costed recommendation with low and
  high values for kWh, money, CO2e and payback, ranked by payback then ease.
  Every assumption comes from `rng/config.py`.
- **Evaluation:** 29 cases built from the generator's ground truth, run
  against the API over HTTP, with a report on decomposition error,
  reconciliation, waste detection, savings accuracy, number faithfulness,
  latency and LLM usage.
- **User interface:** a Streamlit page that calls the API over HTTP and shows
  the summary, a waterfall chart of the five causes, the recommendations, the
  assumptions and the trace.

## AI usage

The code and documents listed above were generated with an AI coding assistant
(Claude Code) working to the team's plan in `WORKPLAN.md` and `CLAUDE.md`.

How it was verified:

- **Tests:** 63 automated tests cover these files; the full suite and the
  linter pass.
- **Validation against truth:** the validator's findings are compared with
  `ground_truth.json` on both the primary and the holdout dataset.
- **Harness checked against wrong answers:** the evaluation harness is run
  against a stand-in API that answers correctly, and then against ones that
  blame a machine first, break additivity, invent a number, answer an
  unanswerable question and miss the leak. Each failure is caught.
- **Interface:** run headless against a stand-in API and started for real.

## Declaration

I declare that this work is original, was produced during the hackathon, uses
no real company data, and shares no code with any other team.

Signed: ____________________    Date: 11 October 2026
