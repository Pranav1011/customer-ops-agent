"""What the operations console relies on: demo tickets that show real outcomes,
ticket subjects, closing an escalation, and eval runs that leave committed
files alone."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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


def test_human_resolves_an_escalation():
    r = run_ticket(
        "Please cancel my subscription.", ticket_id="TCK-ESC-CLOSE", customer_id="CUST-00002"
    )
    assert r["escalated"]
    with session_scope() as s:
        esc_id = s.exec(select(Escalation.id).where(Escalation.ticket_id == "TCK-ESC-CLOSE")).one()
    with TestClient(app) as c:
        open_ids = [e["id"] for e in c.get("/escalations").json()["escalations"]]
        assert esc_id in open_ids

        done = c.post(f"/escalations/{esc_id}/resolve")
        assert done.status_code == 200 and done.json()["status"] == "resolved"
        assert done.json()["ticket_id"] == "TCK-ESC-CLOSE"

        open_ids = [e["id"] for e in c.get("/escalations").json()["escalations"]]
        assert esc_id not in open_ids
        assert c.post(f"/escalations/{esc_id}/resolve").status_code == 409
        assert c.post("/escalations/999999/resolve").status_code == 404


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
