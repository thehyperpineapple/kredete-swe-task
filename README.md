# Autonomous Agent Run Loop & Idempotency Engine

A bounded agent run loop with exact integer credit accounting, client-key
idempotency, and resume-from-failure — FastAPI + SQLite + a single-file vanilla UI.

## Run

```bash
./run.sh          # creates .venv, installs deps, serves http://127.0.0.1:8000
```

Then open <http://127.0.0.1:8000>.

## Test

```bash
make test         # or: .venv/bin/python test_scenarios.py
```

Covers: normal run, duplicate idempotency key, mid-step tool failure + retry,
budget exceeded (both credit and step ceilings), and a concurrent duplicate-key race.

## API

| Method | Path | Behaviour |
| --- | --- | --- |
| `POST` | `/runs` | Create + execute a run. A known `idempotency_key` replays the stored record — nothing re-executes, nothing re-bills. |
| `GET` | `/runs/{run_id}` | Run state, full step ledger, credits used, final output. |
| `POST` | `/runs/{run_id}/retry` | Resume a `FAILED` run from the failed step. 409 if not `FAILED`. |
| `GET` | `/runs` | Recent runs. |
| `GET` | `/health` | Liveness + cost table. |

## Cost table

`PLAN` 2 · `SEARCH` 5 · `WRITE_FILE` 3 · `SYNTHESIZE` 4 — integer credits only.

A default run (`max_steps=5`) plans `PLAN → SEARCH → SEARCH → WRITE_FILE →
SYNTHESIZE` = **19 credits**.

## Test scenarios in the UI

The dashboard has one-click buttons for each: **A** normal run, **B** duplicate
request, **C** forced mid-step tool failure (adds `[FAIL_TOOL]` to the goal), **D**
budget exceeded. Failures surface a **Retry from Step N** button.

- [WALKTHROUGH.md](WALKTHROUGH.md) — the three required scenarios run live against the endpoint, with real transcripts.
- [DECISIONS.md](DECISIONS.md) — design write-up: loop bounding, failure/credit strategy, integer accounting, and the trade-off I'm least sure about.
