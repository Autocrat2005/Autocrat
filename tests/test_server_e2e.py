"""Server lockdown + the Phase 1 exit check: a synthetic lead walks DISCOVERED → STAGED
in dry-run and every transition is visible on an authenticated /ws/v2 stream."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.conftest import TEST_TOKEN

AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def client(make_engine):
    from nexus.web import server
    engine = make_engine()
    server.set_engine(engine)
    with TestClient(server.app) as c:
        yield c, engine


@pytest.mark.parametrize("path", ["/api/history", "/api/bus/history", "/api/suggestions",
                                  "/api/brain", "/api/confirmations", "/api/v2/control"])
def test_protected_endpoints_require_token(client, path):
    c, _ = client
    assert c.get(path).status_code == 401
    assert c.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get(path, headers=AUTH).status_code == 200


def test_command_requires_token(client):
    c, _ = client
    assert c.post("/api/command", json={"command": "pipeline"}).status_code == 401
    r = c.post("/api/command", json={"command": "pipeline"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["success"]


def test_cors_is_not_wildcard(client):
    c, _ = client
    evil = c.get("/api/greeting", headers={"Origin": "https://evil.example"})
    assert evil.headers.get("access-control-allow-origin") is None
    ok = c.get("/api/greeting", headers={"Origin": "http://localhost:5173"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_ws_v2_rejects_bad_first_frame(client):
    c, _ = client
    with c.websocket_connect("/ws/v2") as ws:
        ws.send_json({"type": "auth", "token": "nope"})
        assert ws.receive_json()["code"] == 4401
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401


def test_ws_v2_rejects_non_auth_first_frame(client):
    c, _ = client
    with c.websocket_connect(f"/ws/v2?token={TEST_TOKEN}") as ws:   # URL tokens are ignored
        ws.send_json({"type": "command", "text": "pipeline"})
        assert ws.receive_json()["code"] == 4401


def test_control_endpoint_kill_and_guarded_resume(client):
    c, engine = client
    r = c.post("/api/v2/control", json={"action": "kill", "reason": "test"}, headers=AUTH)
    assert r.json()["kill_switch"] is True
    r = c.post("/api/v2/control", json={"action": "resume"}, headers=AUTH)
    assert r.json().get("requires_confirmation")
    assert engine.runtime.killed


def test_e2e_dry_run_discovered_to_staged_over_ws_v2(client):
    c, engine = client
    assert engine.runtime.run_mode == "dry_run"
    with c.websocket_connect("/ws/v2") as ws:
        ws.send_json({"type": "auth", "token": TEST_TOKEN, "client": "pytest"})
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome" and welcome["run_mode"] == "dry_run"
        assert welcome["profile"] == "core"
        ws.send_json({"type": "subscribe", "topics": ["lead"]})
        assert ws.receive_json()["type"] == "subscribed"

        ws.send_json({"type": "command", "id": "sim-1", "text": "simulate lead"})
        events, result = [], None
        for _ in range(50):
            frame = ws.receive_json()
            if frame["type"] == "command_result":
                result = frame
            elif frame["type"] == "event":
                assert frame["v"] == 2 and frame["topic"] == "lead"
                events.append(frame)
            if result and sum(1 for e in events if e["event"] == "lead.transition") >= 3:
                break

        assert result and result["id"] == "sim-1" and result["result"]["success"], result
        lead_id = result["result"]["lead_id"]
        assert events[0]["event"] == "lead.created" and events[0]["lead_id"] == lead_id
        path = [e["payload"]["to"] for e in events if e["event"] == "lead.transition"]
        assert path == ["TRIAGED", "BUILDING", "STAGED"]
        assert all(e["lead_id"] == lead_id for e in events)
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs)

        # Replay gives the HUD the full history from the store
        ws.send_json({"type": "replay", "lead_id": lead_id})
        replay = ws.receive_json()
        assert [e["to_state"] for e in replay["events"]] == ["DISCOVERED", "TRIAGED", "BUILDING", "STAGED"]

    assert engine.store.get_lead(lead_id)["state"] == "STAGED"
    staged = engine.store.outreach_for_lead(lead_id)
    assert len(staged) == 1 and staged[0]["status"] == "staged"   # staged, never sent in dry-run
