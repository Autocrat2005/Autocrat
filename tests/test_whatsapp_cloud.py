"""WhatsApp Cloud API closer: signed webhooks only, one reply per message, free 24h window only."""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from nexus.core.runtime import RuntimeControl
from nexus.integrations.whatsapp_cloud import GraphClient, WhatsAppCloud, click_to_chat, verify_signature

SECRET = "app-secret-test"


class FakeCloser:
    def __init__(self, store, decision=None):
        self.store, self.calls = store, []
        self.decision = decision or {"reply": "hey! what's your volume like?", "intent": "interested", "hot": False}

    def decide(self, transcript, lead_id=None, channel="WhatsApp"):
        self.calls.append(transcript)
        return dict(self.decision)

    def apply_outcome(self, lead, incoming, decision, alert=True):
        self.store.log_event(lead["id"], "message_in", {"text": incoming, "intent": decision["intent"]})
        if decision["intent"] == "stop":
            self.store.transition(lead["id"], "DNC", reason="stop")


class FakeAlerts:
    def __init__(self):
        self.items = []

    def notify(self, text, priority="orange"):
        self.items.append((priority, text))


def make(store, decision=None):
    sent = []
    client = GraphClient("PNID", "TOKEN", post=lambda url, body, headers: sent.append(body) or {"messages": [{"id": "wamid.out"}]})
    cloud = WhatsAppCloud(store, FakeCloser(store, decision), FakeAlerts(), client=client, sleep=lambda s: None,
                          threaded=False)
    cloud.app_secret, cloud.verify_token = SECRET, "verify-me"
    return cloud, sent


def payload(text, wa_id="971501234567", mid="wamid.1", ts=None, name="Omar"):
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": wa_id, "profile": {"name": name}}],
        "messages": [{"from": wa_id, "id": mid, "timestamp": str(int(ts or time.time())), "type": "text",
                      "text": {"body": text}}]}}]}]}


def signed(body: dict):
    raw = json.dumps(body).encode()
    return raw, "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def test_signature_is_required():
    raw = b'{"a":1}'
    good = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    assert verify_signature(raw, good, SECRET)
    assert not verify_signature(raw, good, "other-secret")
    assert not verify_signature(raw, "", SECRET) and not verify_signature(raw, good, "")


def test_reply_once_inside_window_and_alert(store):
    RuntimeControl().set_run_mode("live", actor="test")
    cloud, sent = make(store)
    raw, sig = signed(payload("hi, saw your email. how does this work?"))
    assert cloud.accept(raw, sig)
    assert cloud.accept(raw, sig)                                  # Meta retry: same message id
    texts = [b for b in sent if b.get("type") == "text"]
    assert len(texts) == 1 and texts[0]["to"] == "971501234567" and texts[0]["context"]["message_id"] == "wamid.1"
    assert any(b.get("status") == "read" for b in sent)
    lead = store.find_lead_by_contact("phone:+971501234567")
    assert lead["source"] == "whatsapp_inbound"
    assert "auto-replied" in cloud.alerts.items[-1][1]


def test_bad_signature_does_nothing(store):
    cloud, sent = make(store)
    raw, _ = signed(payload("hi"))
    assert not cloud.accept(raw, "sha256=deadbeef")
    assert sent == [] and store.find_lead_by_contact("phone:+971501234567") is None


def test_ref_links_chat_to_the_email_lead(store):
    RuntimeControl().set_run_mode("live", actor="test")
    lead, _ = store.upsert_lead(source="uae_brokers", dedupe_key="b1", company="Palm Realty", channel="email",
                                contact="omar@palm.ae", market="UAE")
    cloud, _ = make(store)
    cloud.handle_payload(payload(f"Hi Yash, re Palm Realty (ref {lead['id'][:6]})", mid="wamid.9"))
    events = [e["type"] for e in store.lead_events(lead["id"])]
    assert "whatsapp_linked" in events and "message_in" in events
    assert store.get_lead(lead["id"])["data"]["whatsapp"] == "+971501234567"


def test_outside_window_or_dry_run_sends_nothing(store):
    cloud, sent = make(store)
    cloud.handle_payload(payload("hello?", mid="old", ts=time.time() - 30 * 3600))   # dry run by default
    assert not [b for b in sent if b.get("type") == "text"] and "dry run" in cloud.alerts.items[-1][1]
    RuntimeControl().set_run_mode("live", actor="test")
    cloud.handle_payload(payload("still there?", mid="old2", ts=time.time() - 30 * 3600))
    assert not [b for b in sent if b.get("type") == "text"] and "window closed" in cloud.alerts.items[-1][1]


def test_stop_means_dnc_and_no_more_replies(store):
    RuntimeControl().set_run_mode("live", actor="test")
    cloud, sent = make(store, {"reply": "no worries, all the best", "intent": "stop", "hot": False})
    cloud.handle_payload(payload("stop messaging me", mid="s1"))
    assert store.find_lead_by_contact("phone:+971501234567")["state"] == "DNC"
    before = len(sent)
    cloud.handle_payload(payload("hello again", mid="s2"))
    assert len(sent) == before and "DNC" in cloud.alerts.items[-1][1]


def test_webhook_endpoints(make_engine):
    from nexus.web import server
    engine = make_engine()
    server.set_engine(engine)
    cloud, _ = make(engine.store)
    server.set_whatsapp_cloud(cloud)
    try:
        with TestClient(server.app) as c:
            ok = c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                                     "hub.challenge": "12345"})
            assert ok.status_code == 200 and ok.text == "12345"
            assert c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                                       "hub.challenge": "1"}).status_code == 403
            raw, sig = signed(payload("hi", mid="e1"))
            assert c.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": sig}).status_code == 200
            assert c.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": "sha256=00"}).status_code == 401
    finally:
        server.set_whatsapp_cloud(None)


def test_email_carries_whatsapp_link_for_uae_only(store):
    from tests.test_email_channel import FakeAlerts as EA, FakeCloser as EC, FakeTransport
    from nexus.integrations.email_channel import EmailWorker
    w = EmailWorker(store, EC({"reply": "", "intent": "other"}, store), EA(), transport=FakeTransport(), sleep=lambda s: None)
    w.wa_number, w.wa_markets = "+971 50 000 0000", ["uae"]
    uae, _ = store.upsert_lead(source="uae_brokers", dedupe_key="u1", company="Palm Realty", market="UAE")
    us, _ = store.upsert_lead(source="show_hn", dedupe_key="u2", company="Acme", market="US")
    line = w.whatsapp_line(uae)
    assert line.startswith("or if whatsapp is easier: https://wa.me/971500000000?text=") and uae["id"][:6] in line
    assert w.whatsapp_line(us) == ""
    assert click_to_chat("") == ""
