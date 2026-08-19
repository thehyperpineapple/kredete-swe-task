"""
Autonomous Agent Run Loop & Idempotency Engine.

A minimal, reliable agent run loop:
  - Bounded execution (max_steps + max_credits, checked BEFORE each step).
  - Exact integer credit accounting (no floats anywhere).
  - Client-key idempotency: a replayed POST /runs never re-executes or re-bills.
  - Crash-safe resume: POST /runs/{id}/retry continues a FAILED run from the
    failed step forward, keeping every credit already billed for successful work.

Run with:  ./run.sh   (or: uvicorn app:app --reload)
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("AGENT_DB_PATH", os.path.join(BASE_DIR, "agent_runs.db"))
TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "index.html")

# Artificial per-step latency so the UI timeline is observable. Set to 0 in tests.
STEP_DELAY_SEC = float(os.environ.get("AGENT_STEP_DELAY", "0.45"))

# Integer credit cost table. 1 credit = 1 unit. No floats, ever.
COST_TABLE: Dict[str, int] = {
    "PLAN": 2,
    "SEARCH": 5,
    "WRITE_FILE": 3,
    "SYNTHESIZE": 4,
}

FAIL_SENTINEL = "[FAIL_TOOL]"

RUN_PENDING = "PENDING"
RUN_RUNNING = "RUNNING"
RUN_COMPLETED = "COMPLETED"
RUN_FAILED = "FAILED"
RUN_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"

STEP_RUNNING = "RUNNING"
STEP_SUCCESS = "SUCCESS"
STEP_FAILED = "FAILED"


def now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    goal            TEXT NOT NULL,
    status          TEXT NOT NULL,
    credits_used    INTEGER NOT NULL DEFAULT 0,
    max_credits     INTEGER NOT NULL DEFAULT 50,
    max_steps       INTEGER NOT NULL DEFAULT 5,
    final_output    TEXT,
    error_message   TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idempotency_key
    ON runs (idempotency_key);

CREATE TABLE IF NOT EXISTS steps (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs (id),
    step_number   INTEGER NOT NULL,
    action        TEXT NOT NULL,
    input_payload TEXT NOT NULL,
    output_payload TEXT,
    cost          INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL,
    attempt       INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_steps_run_id ON steps (run_id, step_number);
"""


def connect() -> sqlite3.Connection:
    """One connection per unit of work. WAL keeps readers unblocked by the writer."""
    conn = sqlite3.connect(DB_PATH, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def fetch_run(conn: sqlite3.Connection, run_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def fetch_run_by_key(conn: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs WHERE idempotency_key = ?", (key,)
    ).fetchone()


def fetch_steps(conn: sqlite3.Connection, run_id: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM steps WHERE run_id = ? ORDER BY step_number ASC, created_at ASC",
        (run_id,),
    ).fetchall()


def _json_or_none(raw: Optional[str]) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def serialize(conn: sqlite3.Connection, run_id: str, replayed: bool = False) -> Dict[str, Any]:
    run = fetch_run(conn, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    steps = fetch_steps(conn, run_id)
    billed = sum(int(s["cost"]) for s in steps if s["status"] == STEP_SUCCESS)
    return {
        "run": {
            "id": run["id"],
            "idempotency_key": run["idempotency_key"],
            "goal": run["goal"],
            "status": run["status"],
            "credits_used": int(run["credits_used"]),
            "max_credits": int(run["max_credits"]),
            "max_steps": int(run["max_steps"]),
            "final_output": run["final_output"],
            "error_message": run["error_message"],
            "retry_count": int(run["retry_count"]),
            "created_at": int(run["created_at"]),
            "updated_at": int(run["updated_at"]),
        },
        "steps": [
            {
                "id": s["id"],
                "run_id": s["run_id"],
                "step_number": int(s["step_number"]),
                "action": s["action"],
                "input_payload": _json_or_none(s["input_payload"]),
                "output_payload": _json_or_none(s["output_payload"]),
                "cost": int(s["cost"]),
                "status": s["status"],
                "attempt": int(s["attempt"]),
                "created_at": int(s["created_at"]),
            }
            for s in steps
        ],
        # Authoritative billed total, recomputed from the step ledger. Must always
        # equal runs.credits_used -- exposed separately so drift is visible, not silent.
        "credits_used": int(run["credits_used"]),
        "credits_ledger_total": billed,
        "final_output": run["final_output"],
        # True when this response is a replay of a prior request (nothing re-executed).
        "idempotent_replay": replayed,
    }


# --------------------------------------------------------------------------- #
# Mock agent: deterministic planner + tool runner
# --------------------------------------------------------------------------- #


def build_plan(goal: str, max_steps: int) -> List[str]:
    """
    Deterministic action plan for a goal. Always terminates with SYNTHESIZE.

    The canonical plan is PLAN -> SEARCH -> WRITE_FILE -> SYNTHESIZE (14 credits).
    Extra budget headroom is spent on additional SEARCH passes. The plan is derived
    from (goal, max_steps) only -- never stored -- so a resumed run rebuilds the
    exact same plan and picks up at the right index.
    """
    extra_searches = max(0, max_steps - 4)
    plan = ["PLAN"] + ["SEARCH"] * (1 + extra_searches) + ["WRITE_FILE", "SYNTHESIZE"]
    return plan


def run_tool(action: str, goal: str, step_number: int, attempt: int) -> Dict[str, Any]:
    """
    Deterministic mock tool dispatcher.

    Failure injection: when the goal contains [FAIL_TOOL], the SEARCH tool at step 2
    raises on its FIRST attempt only. A retry of that same step succeeds, which is
    what makes Scenario C's "retry from failed step" observable end to end.
    """
    if (
        FAIL_SENTINEL in goal
        and action == "SEARCH"
        and step_number == 2
        and attempt == 1
    ):
        raise RuntimeError(
            "SearchTool: upstream provider returned 503 (injected failure via "
            f"{FAIL_SENTINEL} at step {step_number})"
        )

    clean_goal = goal.replace(FAIL_SENTINEL, "").strip()

    if action == "PLAN":
        return {
            "summary": f"Decomposed goal into an executable plan: {clean_goal[:120]}",
            "subtasks": [
                "Gather source material",
                "Draft working notes",
                "Synthesize final answer",
            ],
        }
    if action == "SEARCH":
        return {
            "summary": f"Retrieved 3 sources relevant to: {clean_goal[:120]}",
            "results": [
                {"title": "Agent loop architectures", "score": 92},
                {"title": "Bounded execution & budgets", "score": 88},
                {"title": "Idempotency in job runners", "score": 81},
            ],
        }
    if action == "WRITE_FILE":
        return {
            "summary": "Wrote working notes to notes/draft.md",
            "path": "notes/draft.md",
            "bytes": 1024,
        }
    if action == "SYNTHESIZE":
        return {
            "summary": "Synthesized the final answer from prior step outputs.",
            "final_output": (
                f"Completed goal: {clean_goal}\n\n"
                "Findings: bounded agent loops need three invariants -- a hard step "
                "ceiling, a pre-flight credit check, and an append-only step ledger "
                "so that a crash mid-run never double-bills on resume."
            ),
        }
    raise ValueError(f"Unknown action: {action}")


# --------------------------------------------------------------------------- #
# Run loop
# --------------------------------------------------------------------------- #

# One lock per run id: prevents a duplicate POST or an eager retry from driving the
# same run concurrently inside this process. The DB status guards below are the
# authority (they'd hold across processes too); this lock just avoids wasted work.
_run_locks: Dict[str, threading.Lock] = {}
_run_locks_guard = threading.Lock()


def _lock_for(run_id: str) -> threading.Lock:
    with _run_locks_guard:
        return _run_locks.setdefault(run_id, threading.Lock())


def _finish(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    final_output: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    conn.execute(
        """UPDATE runs
              SET status = ?, final_output = ?, error_message = ?, updated_at = ?
            WHERE id = ?""",
        (status, final_output, error_message, now_ms(), run_id),
    )


def _resume_point(conn: sqlite3.Connection, run_id: str) -> Tuple[int, int]:
    """(next step number, attempt number for that step) derived from the ledger."""
    row = conn.execute(
        "SELECT COALESCE(MAX(step_number), 0) AS n FROM steps "
        "WHERE run_id = ? AND status = ?",
        (run_id, STEP_SUCCESS),
    ).fetchone()
    next_step = int(row["n"]) + 1
    prior = conn.execute(
        "SELECT COUNT(*) AS c FROM steps WHERE run_id = ? AND step_number = ?",
        (run_id, next_step),
    ).fetchone()
    return next_step, int(prior["c"]) + 1


def execute_run(run_id: str) -> None:
    """
    Drive a run to a terminal state. Safe to call on a fresh run or a resumed one:
    the starting point is always recomputed from the persisted step ledger, never
    from in-memory state, so a process restart loses nothing but the in-flight step.
    """
    lock = _lock_for(run_id)
    if not lock.acquire(blocking=False):
        return  # another worker already owns this run
    conn = connect()
    try:
        # Atomic claim. Exactly one worker can move a run out of PENDING, so a
        # duplicate POST, an eager retry, or a second uvicorn worker cannot drive
        # the same run concurrently. This -- not the in-process lock -- is the
        # correctness boundary, and it holds across processes.
        claimed = conn.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (RUN_RUNNING, now_ms(), run_id, RUN_PENDING),
        )
        if claimed.rowcount != 1:
            return

        run = fetch_run(conn, run_id)
        if run is None:
            return

        goal = run["goal"]
        max_steps = int(run["max_steps"])
        max_credits = int(run["max_credits"])
        plan = build_plan(goal, max_steps)

        step_number, attempt = _resume_point(conn, run_id)

        while True:
            run = fetch_run(conn, run_id)
            credits_used = int(run["credits_used"])

            # Plan exhausted -> the agent is done.
            if step_number > len(plan):
                _finish(conn, run_id, RUN_COMPLETED, final_output=run["final_output"])
                return

            # --- Guard 1: step ceiling (checked BEFORE executing step N) ---
            if step_number > max_steps:
                _finish(
                    conn,
                    run_id,
                    RUN_BUDGET_EXCEEDED,
                    error_message=(
                        f"Step limit reached: step {step_number} exceeds max_steps="
                        f"{max_steps}. Billed {credits_used} credits for completed work."
                    ),
                )
                return

            action = plan[step_number - 1]
            cost = COST_TABLE[action]

            # --- Guard 2: credit ceiling (checked BEFORE spending) ---
            if credits_used + cost > max_credits:
                _finish(
                    conn,
                    run_id,
                    RUN_BUDGET_EXCEEDED,
                    error_message=(
                        f"Credit limit reached: step {step_number} ({action}) costs "
                        f"{cost}, which would take usage to {credits_used + cost} of "
                        f"max_credits={max_credits}. Billed {credits_used} credits."
                    ),
                )
                return

            # Open the step as RUNNING with cost 0. Nothing is billed until the tool
            # returns successfully, so an abandoned step can never leave a charge.
            step_id = str(uuid.uuid4())
            input_payload = json.dumps(
                {"goal": goal, "action": action, "step": step_number, "attempt": attempt}
            )
            conn.execute(
                """INSERT INTO steps
                       (id, run_id, step_number, action, input_payload,
                        output_payload, cost, status, attempt, created_at)
                   VALUES (?, ?, ?, ?, ?, NULL, 0, ?, ?, ?)""",
                (
                    step_id,
                    run_id,
                    step_number,
                    action,
                    input_payload,
                    STEP_RUNNING,
                    attempt,
                    now_ms(),
                ),
            )

            if STEP_DELAY_SEC:
                time.sleep(STEP_DELAY_SEC)

            try:
                output = run_tool(action, goal, step_number, attempt)
            except Exception as exc:  # tool raised -> bill nothing for this step
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        """UPDATE steps
                              SET status = ?, cost = 0,
                                  output_payload = ?
                            WHERE id = ?""",
                        (
                            STEP_FAILED,
                            json.dumps({"error": str(exc), "type": type(exc).__name__}),
                            step_id,
                        ),
                    )
                    _finish(
                        conn,
                        run_id,
                        RUN_FAILED,
                        final_output=run["final_output"],
                        error_message=f"Step {step_number} ({action}) failed: {exc}",
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                return

            # --- Atomic commit point: step result + credit debit in one transaction.
            # Either the step is SUCCESS and the credits are debited, or neither.
            final_output = run["final_output"]
            if action == "SYNTHESIZE":
                final_output = output.get("final_output")

            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE steps SET status = ?, cost = ?, output_payload = ? WHERE id = ?",
                    (STEP_SUCCESS, cost, json.dumps(output), step_id),
                )
                conn.execute(
                    """UPDATE runs
                          SET credits_used = credits_used + ?,
                              final_output = ?,
                              updated_at = ?
                        WHERE id = ?""",
                    (cost, final_output, now_ms(), run_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

            step_number += 1
            attempt = 1
    except Exception as exc:  # loop-level failure: never leave a run stuck RUNNING
        try:
            _finish(conn, run_id, RUN_FAILED, error_message=f"Run loop error: {exc}")
        except Exception:
            pass
    finally:
        conn.close()
        lock.release()


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


class CreateRunRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=200)
    goal: str = Field(..., min_length=1, max_length=4000)
    max_steps: int = Field(5, ge=1, le=50)
    max_credits: int = Field(50, ge=0, le=100000)


app = FastAPI(title="Autonomous Agent Run Loop", version="1.0.0")

init_db()


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "cost_table": COST_TABLE}


@app.post("/runs")
def create_run(req: CreateRunRequest, background: BackgroundTasks) -> Dict[str, Any]:
    conn = connect()
    try:
        # Fast path: known key -> replay the stored record, execute nothing.
        existing = fetch_run_by_key(conn, req.idempotency_key)
        if existing is not None:
            return serialize(conn, existing["id"], replayed=True)

        run_id = str(uuid.uuid4())
        ts = now_ms()
        try:
            conn.execute(
                """INSERT INTO runs
                       (id, idempotency_key, goal, status, credits_used, max_credits,
                        max_steps, final_output, error_message, retry_count,
                        created_at, updated_at)
                   VALUES (?, ?, ?, ?, 0, ?, ?, NULL, NULL, 0, ?, ?)""",
                (
                    run_id,
                    req.idempotency_key,
                    req.goal,
                    RUN_PENDING,
                    req.max_credits,
                    req.max_steps,
                    ts,
                    ts,
                ),
            )
        except sqlite3.IntegrityError:
            # Lost a race against a concurrent identical request. The UNIQUE index --
            # not the SELECT above -- is what actually guarantees exactly-once.
            existing = fetch_run_by_key(conn, req.idempotency_key)
            if existing is None:
                raise
            return serialize(conn, existing["id"], replayed=True)

        background.add_task(execute_run, run_id)
        return serialize(conn, run_id, replayed=False)
    finally:
        conn.close()


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> Dict[str, Any]:
    conn = connect()
    try:
        return serialize(conn, run_id)
    finally:
        conn.close()


@app.get("/runs")
def list_runs(limit: int = 20) -> Dict[str, Any]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, goal, status, credits_used, max_credits, created_at "
            "FROM runs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        ).fetchall()
        return {"runs": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/runs/{run_id}/retry")
def retry_run(run_id: str, background: BackgroundTasks) -> Dict[str, Any]:
    conn = connect()
    try:
        run = fetch_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        if run["status"] != RUN_FAILED:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Run is {run['status']}; only FAILED runs can be retried. "
                    "Completed and budget-exceeded runs are terminal."
                ),
            )

        # Atomic claim: exactly one caller flips FAILED -> RUNNING. A second
        # concurrent retry updates 0 rows and is rejected instead of double-running.
        cur = conn.execute(
            """UPDATE runs
                  SET status = ?, error_message = NULL,
                      retry_count = retry_count + 1, updated_at = ?
                WHERE id = ? AND status = ?""",
            (RUN_PENDING, now_ms(), run_id, RUN_FAILED),
        )
        if cur.rowcount != 1:
            raise HTTPException(status_code=409, detail="Run is already being retried")

        # credits_used is deliberately untouched: work already billed stays billed.
        background.add_task(execute_run, run_id)
        return serialize(conn, run_id)
    finally:
        conn.close()
