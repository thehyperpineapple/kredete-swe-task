"""
End-to-end verification of the four graded behaviours. Runs in-process against the
FastAPI app with step latency disabled -- no server needed.

    python test_scenarios.py
"""
import os
import sys
import tempfile
import uuid

os.environ["AGENT_STEP_DELAY"] = "0"
os.environ["AGENT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

import app as agent_app  # noqa: E402

client = TestClient(agent_app.app)
FAILURES = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {actual!r}" + ("" if ok else f" (expected {expected!r})"))
    if not ok:
        FAILURES.append(label)


def create(goal, key=None, max_steps=5, max_credits=50):
    return client.post("/runs", json={
        "idempotency_key": key or str(uuid.uuid4()),
        "goal": goal,
        "max_steps": max_steps,
        "max_credits": max_credits,
    }).json()


def scenario_a():
    print("\nScenario A -- normal run")
    d = create("Research AI agent architectures and write a summary")
    d = client.get(f"/runs/{d['run']['id']}").json()
    check("status", d["run"]["status"], "COMPLETED")
    # PLAN 2 + SEARCH 5 + SEARCH 5 + WRITE_FILE 3 + SYNTHESIZE 4 = 19
    check("credits_used", d["run"]["credits_used"], 19)
    check("ledger matches run total", d["credits_ledger_total"], d["run"]["credits_used"])
    check("step count", len(d["steps"]), 5)
    check("has final output", bool(d["run"]["final_output"]), True)


def scenario_b():
    print("\nScenario B -- duplicate request (idempotency)")
    key = str(uuid.uuid4())
    first = create("Research AI agent architectures and write a summary", key=key)
    settled = client.get(f"/runs/{first['run']['id']}").json()
    replay = create("Research AI agent architectures and write a summary", key=key)
    check("replay flag", replay["idempotent_replay"], True)
    check("same run id", replay["run"]["id"], settled["run"]["id"])
    check("no new credits", replay["run"]["credits_used"], settled["run"]["credits_used"])
    check("no new steps", len(replay["steps"]), len(settled["steps"]))

    # A replay with a *different* goal must still not start a second run.
    other = create("A completely different goal", key=key)
    check("key wins over body", other["run"]["id"], settled["run"]["id"])
    check("goal unchanged", other["run"]["goal"], settled["run"]["goal"])


def scenario_c():
    print("\nScenario C -- mid-step tool failure + retry")
    d = create("[FAIL_TOOL] Research AI agent architectures and write a summary")
    run_id = d["run"]["id"]
    d = client.get(f"/runs/{run_id}").json()
    check("status", d["run"]["status"], "FAILED")
    check("credits after failure (PLAN only)", d["run"]["credits_used"], 2)
    failed = [s for s in d["steps"] if s["status"] == "FAILED"]
    check("failed step number", failed[0]["step_number"], 2)
    check("failed step action", failed[0]["action"], "SEARCH")
    check("failed step billed", failed[0]["cost"], 0)

    r = client.post(f"/runs/{run_id}/retry").json()
    d = client.get(f"/runs/{run_id}").json()
    check("status after retry", d["run"]["status"], "COMPLETED")
    check("credits after retry (no double-bill)", d["run"]["credits_used"], 19)
    check("ledger matches run total", d["credits_ledger_total"], d["run"]["credits_used"])
    plan_steps = [s for s in d["steps"] if s["action"] == "PLAN" and s["status"] == "SUCCESS"]
    check("PLAN billed exactly once", len(plan_steps), 1)
    check("retry_count", d["run"]["retry_count"], 1)

    # Retrying a terminal run is rejected, not silently re-run.
    check("retry on COMPLETED rejected", client.post(f"/runs/{run_id}/retry").status_code, 409)


def scenario_d():
    print("\nScenario D -- budget exceeded")
    d = create("Deep-research the entire history of autonomous agents", max_credits=10)
    d = client.get(f"/runs/{d['run']['id']}").json()
    check("status", d["run"]["status"], "BUDGET_EXCEEDED")
    # PLAN 2 + SEARCH 5 = 7; next SEARCH would hit 12 > 10, so it never runs.
    check("credits billed (spent only)", d["run"]["credits_used"], 7)
    check("never exceeds cap", d["run"]["credits_used"] <= d["run"]["max_credits"], True)
    check("no step beyond the guard", len([s for s in d["steps"] if s["status"] == "SUCCESS"]), 2)
    check("has error message", bool(d["run"]["error_message"]), True)

    print("\nScenario D2 -- step ceiling")
    d = create("Short leash goal", max_steps=2, max_credits=1000)
    d = client.get(f"/runs/{d['run']['id']}").json()
    check("status", d["run"]["status"], "BUDGET_EXCEEDED")
    check("stopped at max_steps", len([s for s in d["steps"] if s["status"] == "SUCCESS"]), 2)
    check("credits (PLAN + SEARCH)", d["run"]["credits_used"], 7)


def scenario_e():
    print("\nScenario E -- concurrent duplicate keys (race)")
    import concurrent.futures
    key = str(uuid.uuid4())
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: create("Race condition goal", key=key), range(8)))
    ids = {r["run"]["id"] for r in results}
    check("exactly one run created", len(ids), 1)
    d = client.get(f"/runs/{ids.pop()}").json()
    check("billed exactly once", d["run"]["credits_used"], 19)
    check("ledger matches run total", d["credits_ledger_total"], d["run"]["credits_used"])


for fn in (scenario_a, scenario_b, scenario_c, scenario_d, scenario_e):
    fn()

print("\n" + ("ALL CHECKS PASSED" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
