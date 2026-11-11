"""What the operations console relies on: demo tickets that show real outcomes,
ticket subjects, closing an escalation, and eval runs that leave committed
files alone."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from agent_ops.agent.graph import run_ticket
from agent_ops.api.main import app
from agent_ops.api.routes import ticket_subject
from agent_ops.backend.db import session_scope
from agent_ops.backend.models import Escalation, Ticket


def test_subject_is_kept_or_taken_from_the_first_sentence():
    assert ticket_subject("  Late parcel ", "anything") == "Late parcel"
    assert ticket_subject("", "Where is my order ORD-000001? It hasn't arrived.") == (
        "Where is my order ORD-000001?"
    )
    long = ticket_subject("", "word " * 40)
    assert len(long) <= 60 and long.endswith("…")


def test_submitted_ticket_without_subject_gets_one():
    with TestClient(app) as c:
        ref = c.post(
            "/jobs",
            json={"body": "Where is my order ORD-000001? Thanks.", "customer_id": "CUST-00001"},
        ).json()
        assert c.get(f"/tickets/{ref['ticket_id']}").json()["subject"] == (
            "Where is my order ORD-000001?"
        )


def test_demo_tickets_show_actions_and_human_handoffs():
    with session_scope() as s:
        seeded = [
            (t.id, t.body, t.customer_id)
            for t in s.exec(select(Ticket).where(Ticket.id.like("TCK-0000%"))).all()
        ]
    assert len(seeded) == 6
    outcomes = {}
    for tid, body, cid in seeded:
        r = run_ticket(body, ticket_id=f"{tid}-demo", customer_id=cid)
        outcomes[tid] = (r["status"], r["intent"])
    assert outcomes == {
        "TCK-000001": ("resolved", "order_status"),
        "TCK-000002": ("resolved", "refund"),
        "TCK-000003": ("escalated", "cancel_subscription"),
        "TCK-000004": ("resolved", "double_charge"),
        "TCK-000005": ("resolved", "address_change"),
        "TCK-000006": ("escalated", "damaged_item"),
    }


@pytest.fixture
def operator_token(monkeypatch):
    from agent_ops.config import get_settings

    monkeypatch.setenv("AURORA_OPERATOR_TOKEN", "test-operator-token")
    get_settings.cache_clear()
    yield "test-operator-token"
    monkeypatch.delenv("AURORA_OPERATOR_TOKEN")
    get_settings.cache_clear()


def _escalate(ticket_id: str) -> tuple[int, str]:
    r = run_ticket("Please cancel my subscription.", ticket_id=ticket_id, customer_id="CUST-00002")
    assert r["escalated"]
    with session_scope() as s:
        esc_id = s.exec(select(Escalation.id).where(Escalation.ticket_id == ticket_id)).one()
    return esc_id, r["run_id"]


def test_closing_an_escalation_is_disabled_without_an_operator_token():
    esc_id, _ = _escalate("TCK-ESC-NOTOKEN")
    with TestClient(app) as c:
        r = c.post(f"/escalations/{esc_id}/resolve", json={"note": "refunded by hand"})
        assert r.status_code == 403
        assert "AURORA_OPERATOR_TOKEN" in r.json()["detail"]
        open_ids = [e["id"] for e in c.get("/escalations").json()["escalations"]]
        assert esc_id in open_ids


def test_operator_closes_an_escalation_with_a_note(operator_token):
    esc_id, run_id = _escalate("TCK-ESC-CLOSE")
    url = f"/escalations/{esc_id}/resolve"
    good = {"X-Operator-Token": operator_token}
    with TestClient(app) as c:
        assert (
            c.post(url, json={"note": "done"}, headers={"X-Operator-Token": "nope"}).status_code
            == 401
        )
        assert c.post(url, json={"note": "done"}).status_code == 401
        assert c.post(url, json={}, headers=good).status_code == 422

        done = c.post(
            url, json={"note": "Cancelled after a call", "resolved_by": "sam"}, headers=good
        )
        assert done.status_code == 200
        body = done.json()
        assert body["status"] == "resolved" and body["ticket_id"] == "TCK-ESC-CLOSE"
        assert body["note"] == "Cancelled after a call" and body["resolved_by"] == "sam"
        assert body["resolved_at"]

        open_ids = [e["id"] for e in c.get("/escalations").json()["escalations"]]
        assert esc_id not in open_ids
        assert c.post(url, json={"note": "again"}, headers=good).status_code == 409
        assert (
            c.post("/escalations/999999/resolve", json={"note": "x y z"}, headers=good).status_code
            == 404
        )

        events = c.get(f"/trace/{run_id}").json()["events"]
        assert events[-1]["type"] == "human_resolved"
        assert events[-1]["note"] == "Cancelled after a call"


def test_concurrent_closes_have_one_winner(operator_token):
    from concurrent.futures import ThreadPoolExecutor

    esc_id, _ = _escalate("TCK-ESC-RACE")
    with TestClient(app) as c:

        def close(i: int) -> int:
            return c.post(
                f"/escalations/{esc_id}/resolve",
                json={"note": f"closed by worker {i}"},
                headers={"X-Operator-Token": operator_token},
            ).status_code

        with ThreadPoolExecutor(8) as pool:
            codes = sorted(pool.map(close, range(8)))
    assert codes == [200] + [409] * 7


def test_init_db_adds_escalation_columns_to_an_older_database(tmp_path):
    import sqlalchemy

    import agent_ops.backend.db as db

    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE escalation (id INTEGER PRIMARY KEY, ticket_id VARCHAR, "
            "customer_id VARCHAR, run_id VARCHAR, reason VARCHAR, context JSON, "
            "status VARCHAR, created_at DATETIME)"
        )
    saved = db._engine
    db._engine = engine
    try:
        db.init_db()
        with engine.begin() as conn:
            cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(escalation)")}
    finally:
        db._engine = saved
    assert {"resolved_by", "resolved_at", "note"} <= cols


def test_kb_import_leaves_no_stray_telemetry_file(tmp_path):
    # With $HOME unwritable, ONNX Runtime used to drop ":memory:.ses" in the cwd.
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o500)
    env = {**os.environ, "HOME": str(home)}
    env.pop("ORT_DISABLE_TELEMETRY", None)
    src = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(src), env.get("PYTHONPATH")]))
    subprocess.run(
        [sys.executable, "-c", "import agent_ops.backend.kb"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
    )
    assert not list(tmp_path.glob("*.ses"))
