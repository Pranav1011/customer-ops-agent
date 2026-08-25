"""API tests: the async job queue (submit -> poll -> result) and /metrics."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from agent_ops.api.main import app


def _poll(client: TestClient, job_id: str, tries: int = 80) -> dict:
    for _ in range(tries):
        j = client.get(f"/jobs/{job_id}").json()
        if j["status"] in ("succeeded", "failed"):
            return j
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish")


def test_async_job_queue_resolves_and_traces():
    with TestClient(app) as c:
        r = c.post(
            "/jobs",
            json={
                "body": "Where is my order ORD-000001?",
                "customer_id": "CUST-00001",
                "order_id": "ORD-000001",
            },
        )
        assert r.status_code == 202
        ref = r.json()
        assert ref["status"] == "queued" and ref["job_id"].startswith("JOB-")

        job = _poll(c, ref["job_id"])
        assert job["status"] == "succeeded"
        assert job["result"]["run_id"]
        # The run's trace is retrievable.
        assert c.get(f"/trace/{job['result']['run_id']}").status_code == 200


def test_rerun_existing_ticket_via_queue():
    with TestClient(app) as c:
        # seeded open ticket TCK-000001 exists
        r = c.post("/tickets/TCK-000001/rerun")
        assert r.status_code == 202
        job = _poll(c, r.json()["job_id"])
        assert job["status"] == "succeeded"


def test_metrics_shape():
    with TestClient(app) as c:
        m = c.get("/metrics").json()
        assert set(["runs", "escalation_rate", "avg_cost_usd", "jobs", "actions"]) <= set(m)
        assert m["runs"]["total"] >= 0
        assert 0.0 <= m["escalation_rate"] <= 1.0
