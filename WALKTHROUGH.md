# Walkthrough

Every transcript below is real output from `curl` against a running server, not
illustrative pseudo-output. (Transcripts were captured against a server on port 8077 to avoid a local clash;
`run.sh` defaults to 8000, so the URLs below use 8000. Nothing else differs.)

Reproduce with:

```bash
./run.sh                      # terminal 1
make test                     # terminal 2 -- 30 assertions, all scenarios
```

Cost table: `PLAN` 2 · `SEARCH` 5 · `WRITE_FILE` 3 · `SYNTHESIZE` 4.
A 5-step run plans `PLAN → SEARCH → SEARCH → WRITE_FILE → SYNTHESIZE` = **19 credits**.

---

## Scenario 1 — a goal that completes

```bash
curl -X POST localhost:8000/runs -H 'Content-Type: application/json' -d '{
  "idempotency_key": "demo-key-001",
  "goal": "Research AI agent architectures and write a summary",
  "max_steps": 5, "max_credits": 50 }'
```

The POST returns immediately with the run claimed but not yet started:

```json
{ "status": "PENDING", "credits_used": 0, "idempotent_replay": false, "steps": 0 }
```

`GET /runs/{id}` once it settles:

```json
{
  "status": "COMPLETED",
  "credits_used": 19,
  "credits_ledger_total": 19,
  "steps": [
    { "n": 1, "action": "PLAN",       "cost": 2, "status": "SUCCESS" },
    { "n": 2, "action": "SEARCH",     "cost": 5, "status": "SUCCESS" },
    { "n": 3, "action": "SEARCH",     "cost": 5, "status": "SUCCESS" },
    { "n": 4, "action": "WRITE_FILE", "cost": 3, "status": "SUCCESS" },
    { "n": 5, "action": "SYNTHESIZE", "cost": 4, "status": "SUCCESS" }
  ]
}
```

**Final output** (`run.final_output`):

> Completed goal: Research AI agent architectures and write a summary
>
> Findings: bounded agent loops need three invariants — a hard step ceiling, a
> pre-flight credit check, and an append-only step ledger so that a crash mid-run
> never double-bills on resume.

**Credits used: 19.** `credits_used` is the running balance incremented in SQL;
`credits_ledger_total` is recomputed by summing the successful step costs. They are
returned separately on purpose — if the two ever diverged, the bug would be visible
in the API response rather than silently mis-billing.

---

## Scenario 2 — the client retries the exact same request

The first response never arrived, so the client re-sends the identical body,
including `"idempotency_key": "demo-key-001"`:

```json
{
  "run_id": "8df6ab47-fd74-40de-bf50-0b562a2e66a6",
  "status": "COMPLETED",
  "credits_used": 19,
  "idempotent_replay": true,
  "steps": 5
}
```

- Same `run_id` as scenario 1: **YES**
- Runs in the database: **1**
- Credits: **still 19** — not 38
- Steps: **still 5** — no second execution

The same key with a *different* goal also returns the original run, unchanged:

```json
{ "run_id": "8df6ab47-…", "goal": "Research AI agent architectures and write a summary",
  "credits_used": 19, "idempotent_replay": true }
```

That is deliberate. The key identifies the *request*, so a client retry that
garbles its body still cannot start a second billable run. The alternative — 409 on
body mismatch — is defensible, but it turns a client-side bug into a failed run
rather than a safe no-op.

### The race, not just the sequential case

A sequential replay is the easy half. The real failure mode is two retries landing
at once, before either has committed. Eight concurrent identical POSTs:

```
distinct run ids returned: 1
{ "status": "COMPLETED", "credits_used": 19, "credits_ledger_total": 19, "steps": 5 }
```

**Mechanism.** The `SELECT` on `idempotency_key` is only a fast path — it is *not*
the guarantee, because two requests can both miss it. The guarantee is the `UNIQUE`
index on `runs.idempotency_key`: exactly one `INSERT` wins, the losers catch
`IntegrityError` and return the winner's record. A second gate covers execution:
runs are inserted `PENDING`, and the worker claims one with

```sql
UPDATE runs SET status='RUNNING' WHERE id=? AND status='PENDING'
```

`rowcount != 1` means someone else owns it, so the duplicate worker exits. Both
gates are in the database, so they hold across processes — not just across threads.

---

## Scenario 3 — a tool fails partway

`[FAIL_TOOL]` in the goal makes the `SEARCH` tool at step 2 raise, *after* step 1
has already run and already consumed credits.

```json
{
  "status": "FAILED",
  "credits_used": 2,
  "error": "Step 2 (SEARCH) failed: SearchTool: upstream provider returned 503 …",
  "steps": [
    { "n": 1, "action": "PLAN",   "cost": 2, "status": "SUCCESS", "attempt": 1 },
    { "n": 2, "action": "SEARCH", "cost": 0, "status": "FAILED",  "attempt": 1 }
  ]
}
```

**End state: `FAILED`.** **Credits: 2, not 7.**

That is the call I made: **work that succeeded stays billed; work that failed is
never billed.** Step 1 really did run and really did consume resources, so the
customer keeps paying for it and keeps its output. Step 2 produced nothing, so it
costs nothing — its row is retained at `cost: 0` purely as an audit record.

The mechanism is that cost is not written when a step starts. A step is inserted
`RUNNING` with `cost = 0`, and the cost is written *only* in the same transaction
that marks it `SUCCESS`. So there is no window in which a step is billed but
unfinished — and a process that dies mid-step leaves a `RUNNING` row worth 0
credits, which resume simply ignores.

### Recovery

```bash
curl -X POST localhost:8000/runs/{id}/retry
```

```json
{ "status": "PENDING", "credits_used": 2, "retry_count": 1 }
```

After it settles:

```json
{
  "status": "COMPLETED", "credits_used": 19, "credits_ledger_total": 19,
  "steps": [
    { "n": 1, "action": "PLAN",       "cost": 2, "status": "SUCCESS", "attempt": 1 },
    { "n": 2, "action": "SEARCH",     "cost": 0, "status": "FAILED",  "attempt": 1 },
    { "n": 2, "action": "SEARCH",     "cost": 5, "status": "SUCCESS", "attempt": 2 },
    { "n": 3, "action": "SEARCH",     "cost": 5, "status": "SUCCESS", "attempt": 1 },
    { "n": 4, "action": "WRITE_FILE", "cost": 3, "status": "SUCCESS", "attempt": 1 },
    { "n": 5, "action": "SYNTHESIZE", "cost": 4, "status": "SUCCESS", "attempt": 1 }
  ]
}
```

The important number is **19, not 21**. Step 1 was not re-executed and not
re-billed. The retry resumed at step 2 — the failed step — and the ledger now shows
both attempts, so the audit trail records what actually happened rather than hiding
the failure.

**Mechanism.** Resume point is derived from the persisted ledger, never from memory:
`MAX(step_number WHERE status='SUCCESS') + 1`. That is why it survives a process
restart. `credits_used` is deliberately untouched by the retry endpoint. Retry is
claimed atomically with `WHERE id=? AND status='FAILED'`, so a double-click on
"Retry" cannot double-drive the run — the second one gets a 409:

```
HTTP 409 {"detail":"Run is COMPLETED; only FAILED runs can be retried.
          Completed and budget-exceeded runs are terminal."}
```

---

## Bounding the loop

Two guards run **before** step *N* executes and before any credit is spent. Checking
pre-flight rather than post-hoc is what makes the cap hard — the run cannot overshoot
and then apologise.

**Credit ceiling** (`max_credits: 10`):

```json
{ "status": "BUDGET_EXCEEDED", "credits_used": 7, "max_credits": 10,
  "error": "Credit limit reached: step 3 (SEARCH) costs 5, which would take usage
            to 12 of max_credits=10. Billed 7 credits.",
  "billed_steps": 2 }
```

**Step ceiling** (`max_steps: 2`):

```json
{ "status": "BUDGET_EXCEEDED", "credits_used": 7, "max_steps": 2,
  "error": "Step limit reached: step 3 exceeds max_steps=2. Billed 7 credits for
            completed work." }
```

In both cases the run stops at a clean terminal state, bills exactly what it spent,
and never exceeds the cap. `BUDGET_EXCEEDED` is terminal and deliberately *not*
retryable — resuming a run that hit its budget is a new run with a new budget, not a
retry, and pretending otherwise is how a hard cap quietly becomes a soft one.

A run reaches exactly one of three terminal states: plan exhausted (`COMPLETED`),
guard tripped (`BUDGET_EXCEEDED`), tool raised (`FAILED`). The loop-level `except`
also forces a terminal state, so no run is ever stranded in `RUNNING` by a bug.

---

## What I would fix next

Under multiple uvicorn workers, a worker that crashes mid-run leaves that run
`RUNNING` with nothing to reap it, and nothing currently reclaims it. The fix is a
`lease_expires_at` column plus a sweeper that returns expired `RUNNING` runs to
`FAILED` so they become retryable. I left it out because it needs a background
scheduler this scope doesn't justify — but it is the first thing I would add before
this saw real traffic. See `DECISIONS.md` §4.
