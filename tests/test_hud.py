"""The command deck: snapshot is honest counts from the store; buttons go through the same gates."""

import pytest
from fastapi.testclient import TestClient

from nexus.core.runtime import RuntimeControl
from tests.conftest import TEST_TOKEN

AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def client(make_engine):
    from nexus.web import server
    engine = make_engine()
    server.set_engine(engine)
    with TestClient(server.app) as c:
        yield c, engine


def staged(store, key="k1", contact="a@b.com", draft="subject: hi\nhey sam, quick one"):
    lead, _ = store.upsert_lead(source="show_hn", dedupe_key=key, company="Acme", channel="email",
                                contact=contact, market="US", url="javascript:alert(1)")
    store.transition(lead["id"], "TRIAGED")
    out = store.stage_outreach(lead["id"], "email", draft, contact=contact)
    store.transition(lead["id"], "STAGED", payload={"outreach_id": out["id"]})
    return lead, out


def test_hud_page_is_served_and_data_needs_token(client):
    c, _ = client
    page = c.get("/hud")
    assert page.status_code == 200 and "AUTOCRAT" in page.text
    assert c.get("/api/v2/hud").status_code == 401
    assert c.post("/api/v2/hud/action", json={"action": "skip", "id": "x"}).status_code == 401


def test_snapshot_counts_and_queue(client):
    c, engine = client
    staged(engine.store)
    s = c.get("/api/v2/hud", headers=AUTH).json()["result"]
    funnel = {f["label"]: f["n"] for f in s["funnel"]}
    assert funnel["FOUND"] == 1 and funnel["QUALIFIED"] == 1 and funnel["STAGED"] == 1 and funnel["SENT"] == 0
    assert len(s["queue"]) == 1 and s["queue"][0]["company"] == "Acme"
    assert s["today"]["cap"] == 20 and "run_mode" in s["control"]
    assert {"sources", "budget", "feed", "opportunities"} <= set(s)


def test_edit_then_skip_never_sends(client):
    c, engine = client
    lead, out = staged(engine.store)
    r = c.post("/api/v2/hud/action", json={"action": "edit", "id": out["id"], "text": "subject: hi\nhey sam, better"},
               headers=AUTH).json()
    assert r["success"] and engine.store.get_outreach(out["id"])["draft"].endswith("better")
    r = c.post("/api/v2/hud/action", json={"action": "skip", "id": out["id"]}, headers=AUTH).json()
    assert r["success"]
    assert engine.store.get_outreach(out["id"])["status"] == "rejected"
    assert engine.store.get_lead(lead["id"])["state"] == "ARCHIVED"
    # a skipped draft can't be edited or approved afterwards
    assert not c.post("/api/v2/hud/action", json={"action": "edit", "id": out["id"], "text": "x"}, headers=AUTH).json()["success"]


def test_approve_goes_through_engine_command(client):
    c, engine = client
    _, out = staged(engine.store)
    RuntimeControl().set_run_mode("live", actor="test")
    r = c.post("/api/v2/hud/action", json={"action": "approve", "id": out["id"]}, headers=AUTH).json()
    assert r["success"], r
    approved = engine.store.get_outreach(out["id"])
    assert approved["status"] == "approved" and approved["approved_by"] == "hud"


def test_parse_old_text_reports():
    from nexus.web.hud import parse_opportunities
    ideas = parse_opportunities("🧭 x\n\n1. Bugs cost money (wtp 6/10, 4 mentions)\n   who: founders\n"
                                "   sell now: audit $900\n2. Other pain (wtp 2/10, 1 mentions)\n")
    assert ideas == [{"pain": "Bugs cost money", "wtp": 6, "mentions": 4, "service": "audit $900"},
                     {"pain": "Other pain", "wtp": 2, "mentions": 1, "service": ""}]


def test_payment_moves_lead_and_shows_in_attribution(client):
    c, engine = client
    store = engine.store
    lead, out = staged(store, key="pay1", contact="pay@b.com")
    store.approve_outreach(out["id"], "test"); store.transition(lead["id"], "APPROVED")
    store.mark_outreach_sent(out["id"], "email")
    store.log_event(lead["id"], "message_in", {"text": "how much?", "intent": "pricing", "hot": True}, actor="prospect")
    store.transition(lead["id"], "REPLIED")
    r = c.post("/api/v2/hud/action", json={"action": "payment", "id": lead["id"], "text": "$625 USD deposit"},
               headers=AUTH).json()
    assert r["success"] and store.get_lead(lead["id"])["state"] == "DEPOSIT_OPENED"
    c.post("/api/v2/hud/action", json={"action": "payment", "id": lead["id"], "text": "625 final"}, headers=AUTH)
    assert store.get_lead(lead["id"])["state"] == "WON"
    s = c.get("/api/v2/hud", headers=AUTH).json()["result"]
    row = next(a for a in s["attribution"] if a["source"] == "show_hn")
    assert (row["contacted"], row["replied"], row["hot"], row["paid_usd"], row["won"]) == (1, 1, 1, 1250.0, 1)
    assert s["revenue"]["paid"] == {"USD": 1250.0}
    assert s["replies"][0]["intent"] == "pricing" and s["replies"][0]["paid"] == 1250.0
    assert "cap" in s["cap"]


def test_paid_command_from_telegram_or_cli(make_engine):
    engine = make_engine()
    lead, _ = engine.store.upsert_lead(source="manual", dedupe_key="m1", company="Acme")
    r = engine.execute(f"paid {lead['id']} 1,250 usd final", actor="telegram:1")
    assert r["success"], r
    assert engine.store.payments(lead["id"])[0]["amount"] == 1250.0
