# Design Decisions

## 1. Loop bounding

The loop is bounded by two guards that both run **before step N** is executed and
before any credit is spent (`app.py`, `execute_run`):

Scenarios for `BUDGET_EXCEEDED`
1. **Step ceiling** - `if step_number > max_steps` 
2. **Credit ceiling** - `if credits_used + cost > max_credits` 

A step that would breach the budget is never started and hence it is never partially billed.

The action plan is derived deterministically from `(goal, max_steps)` and is never
stored: `PLAN → SEARCH → WRITE_FILE → SYNTHESIZE`, with any step headroom above 4
spent on extra `SEARCH` passes. Because the plan is a pure function of persisted
inputs, a resumed run rebuilds the identical plan and indexes into it at the right
place. A run terminates in exactly one of three ways: plan exhausted (`COMPLETED`),
guard tripped (`BUDGET_EXCEEDED`), or tool raised (`FAILED`). There is no path that
leaves the loop spinning. Tthe loop-level `except` also forces a terminal state, so a
run can never be stranded in `RUNNING`.

## 2. Mid-run failure & credit retention

**Work that succeeded stays billed; work that failed is never billed.**

Each step is inserted as `RUNNING` with `cost = 0`. The cost is written only in the
same transaction that marks the step `SUCCESS`. So the failure modes are:

- Tool raises: step is marked `FAILED` with `cost = 0`, run goes `FAILED`, and
  `credits_used` retains everything earned by prior successful steps.
- Process dies mid-step - the step row is stranded as `RUNNING` with `cost = 0`. It
  contributes nothing to the balance, and resume recomputes from `SUCCESS` rows only.

`POST /runs/{id}/retry` resumes from `MAX(step_number WHERE status = 'SUCCESS') + 1`
which is derived from the persisted ledger, never from in-memory state — and deliberately
does **not** touch `credits_used`. Already-completed steps are neither re-executed
nor re-billed. The failed attempt's row is kept rather than deleted, and the new
attempt is written at the same `step_number` with `attempt = 2`, so the ledger stays
an append-only audit trail that shows *what actually happened*.

Retry is gated by an atomic `UPDATE … WHERE id = ? AND status = 'FAILED'`; a second
concurrent retry updates 0 rows and gets a 409 instead of double-driving the run.
Only `FAILED` runs are retryable. `COMPLETED` and `BUDGET_EXCEEDED` are terminal
(re-running a budget-exceeded run is a new run with a new budget, not a retry).

The injected `[FAIL_TOOL]` fault fires only on the **first** attempt at step 2, so
the retry path is observable end to end.

## 3. Exact integer accounting

Credits are `INTEGER` columns, and the only arithmetic
is addition. No floats, no decimals, no percentages anywhere in the billing path.
Nothing can drift, because nothing is ever rounded. (The UI's progress bar does
compute a ratio, but that is display-only and never written back.)

I never read `credits_used` into Python, add to it, and write it back — that read-modify-write
would be a lost-update bug under concurrency. Instead the database does the addition
itself:

```sql
UPDATE runs SET credits_used = credits_used + ?
```

**A step's cost and its result are written together, or not at all.** That `UPDATE`
runs inside the same transaction that marks the step `SUCCESS`. Both land or neither
does. So there is no moment where a step is billed but not recorded, or recorded but
not billed — the two facts cannot disagree, because they are one write.

**The books are checked on every read.** `GET /runs/{id}` returns two numbers that
should always be identical:

| Field | Where it comes from |
| --- | --- |
| `credits_used` | the running balance on the run |
| `credits_ledger_total` | recomputed by summing the successful steps |

If those two ever disagreed, it would mean the balance had drifted from the ledger.
Exposing both makes that visible in the API response instead of silently mis-billing.
The tests assert they match in every scenario.

**Charging once is enforced by the database, not by a lookup.** `POST /runs` does
check whether the idempotency key already exists — but that check is only a fast
path, not the guarantee. Two identical requests can both run that check and both
find nothing before either has written anything.

What actually guarantees it is the `UNIQUE` index on `runs.idempotency_key`. Only one
`INSERT` can win; the losers get an `IntegrityError` and return the winner's run
instead of creating their own. The test suite fires 8 simultaneous identical requests
and asserts exactly one run, billed exactly once.

## 4. The trade-off I am least sure about

**When a step is retried, I keep the failed attempt in the ledger instead of deleting
it.** The retry writes a second row with the same `step_number`, so step 2 appears
twice — once `FAILED` at 0 credits, once `SUCCESS` at 5:

```
n=2  SEARCH  cost=0  FAILED   attempt=1
n=2  SEARCH  cost=5  SUCCESS  attempt=2
```

The upside is an honest history: you can see the run actually failed and what it
cost. Deleting the row would hide that.

The downside is that `step_number` is no longer unique per run, so anything reading
the ledger has to know to filter by status rather than assume one row per step. That
is a small trap I am handing to whoever queries this next.

The alternative is to keep step numbers unique and record retries somewhere else like
an `attempts` table, or an archive of superseded rows. It is cleaner to query, more moving
parts. For a ledger this small I preferred one append-only table, but I would rethink
it the moment anything other than the UI reads from it.

