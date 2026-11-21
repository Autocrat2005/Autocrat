"""Cash: deposit requests by one tap, one gentle chaser, and the follow-through after every call."""

from datetime import datetime, timedelta, timezone

from nexus.core.runtime import RuntimeControl
from nexus.integrations import deals
from nexus.integrations.email_channel import EmailWorker
from tests.test_conversion import RecentTransport, mail, sent_lead
from tests.test_email_channel import FakeAlerts, FakeCloser

NOW = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)          # Tue 12:00 Dubai


def worker(store, t, decision=None):
    w = EmailWorker(store, FakeCloser(decision or {"reply": "great, sending the invoice now", "intent": "ready_to_pay",
                                                   "hot": True}, store), FakeAlerts(), transport=t, sleep=lambda s: None)
    w.auto_send, w.daily_cap, w.hours, w.offset = True, 20, "9-18", 240
    return w


def live():
    RuntimeControl().set_run_mode("live", actor="test")


def test_payment_email_has_invoice_amounts_and_your_wise_tag(store, monkeypatch):
    monkeypatch.setenv("WISE_TAG", "@studio-tag")
    lead, _ = store.upsert_lead(source="x", dedupe_key="p1", name="Sara Khan", company="Dune", contact="sara@dune.example")
    body = deals.payment_email(lead, 625, "USD", "WR-2026-0001", total=1250)
    assert body.startswith("hey Sara,")
    assert "invoice WR-2026-0001" in body and "total: USD 1,250" in body and "deposit now (50%): USD 625" in body
    assert "@studio-tag (search that wisetag" in body and "reply \"bank\"" in body
    assert "—" not in body and "–" not in body
    shop, _ = store.upsert_lead(source="x", dedupe_key="p2", name="Acme Insurance", company="Acme Insurance")
    assert deals.payment_email(shop, 500, "USD", "WR-2026-0002").startswith("hey,\n")   # a business, not a person


def test_ready_to_pay_reply_gets_a_one_tap_deposit_button_and_it_sends_in_thread(store, monkeypatch):
    live()
    t = RecentTransport()
    w = worker(store, t)
    lead, _ = sent_lead(store, w, t, email="sara@dune.example", key="d1")
    store.log_event(lead["id"], "message_out", {"text": "it's $2,500, half to start", "deal_usd": 2500}, actor="closer")
    t.inbox = [mail(uid="9", **{"from": "cto@dune.example"}, message_id="<y@dune.example>", body="yes let's do it")]
    w.handle_replies()
    assert w.alerts.buttons == [[{"text": "📤 Send deposit request (USD 1,250)", "callback_data": f"pay:{lead['id']}"}]]
    assert "ready to pay" in w.alerts.items[-1][1]
    res = deals.request_payment(store, w, lead["id"], now=NOW)
    assert res["ok"] and res["invoice"] == "WR-2026-0001" and res["amount"] == 1250
    sent = t.sent[-1]
    assert sent["to"] == "cto@dune.example"                       # the colleague who said yes gets the invoice
    assert sent["subject"].startswith("Re:") and sent["in_reply_to"]
    assert "deposit now (50%): USD 1,250" in sent["body"]
    assert [p["invoice"] for p in deals.pending_deposits(store)] == ["WR-2026-0001"]
    store.record_payment(lead["id"], 1250, currency="USD", kind="deposit", actor="test")
    assert deals.pending_deposits(store) == []


def test_one_friendly_chaser_then_a_ping_to_you(store, monkeypatch):
    live()
    t = RecentTransport()
    w = worker(store, t)
    lead, _ = sent_lead(store, w, t, email="omar@palm.example", key="c1")
    deals.request_payment(store, w, lead["id"], amount=625, now=NOW)
    before = len(t.sent)
    at = lambda hours: NOW.timestamp() - hours * 3600  # noqa: E731
    with store.transaction() as c:
        c.execute("UPDATE lead_events SET ts=? WHERE lead_id=? AND type='payment_requested'", (at(49), lead["id"]))
    assert deals.chase_payments(w, now=NOW) == 1
    assert len(t.sent) == before + 1 and "invoice came through" in t.sent[-1]["body"]
    assert deals.chase_payments(w, now=NOW) == 0                  # once
    with store.transaction() as c:
        c.execute("UPDATE lead_events SET ts=? WHERE lead_id=? AND type='payment_requested'", (at(99), lead["id"]))
        c.execute("UPDATE lead_events SET ts=? WHERE lead_id=? AND type='payment_chased'", (at(50), lead["id"]))
    assert deals.chase_payments(w, now=NOW) == 1
    assert len(t.sent) == before + 1                              # no second email: you're pinged instead
    assert w.alerts.items[-1][0] == "red" and "still hasn't paid" in w.alerts.items[-1][1]


def test_after_the_call_you_tap_how_it_went_and_it_follows_through(store, monkeypatch):
    live()
    monkeypatch.setenv("OWNER_AVAILABILITY", "Mon-Sat 10-22")
    t = RecentTransport()
    w = worker(store, t)
    lead, _ = sent_lead(store, w, t, email="kim@nest.example", key="m1")
    store.transition(lead["id"], "REPLIED")
    start = NOW - timedelta(hours=1)
    store.log_event(lead["id"], "meeting_booked", {"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "link": "https://x",
                                                   "their_offset": 240}, actor="closer")
    assert deals.ask_meeting_outcomes(w, now=NOW) == 1
    assert deals.ask_meeting_outcomes(w, now=NOW) == 0
    codes = [b["callback_data"] for row in w.alerts.buttons for b in row]
    assert codes == [f"mo:{lead['id']}:{c}" for c in ("won", "think", "noshow", "no")]

    noshow = deals.apply_meeting_outcome(store, w, lead["id"], "noshow", now=NOW)
    assert "missed each other" in t.sent[-1]["body"] and " or " in t.sent[-1]["body"]
    assert "👻" in noshow["note"]
    deals.apply_meeting_outcome(store, w, lead["id"], "think", now=NOW)
    assert "working demo on your own setup" in t.sent[-1]["body"]
    won = deals.apply_meeting_outcome(store, w, lead["id"], "won", now=NOW)
    assert won["buttons"][0][0]["callback_data"] == f"pay:{lead['id']}"
    deals.apply_meeting_outcome(store, w, lead["id"], "no", now=NOW)
    assert store.get_lead(lead["id"])["state"] == "LOST"


def test_money_board_lists_what_is_on_the_table(store):
    live()
    t = RecentTransport()
    w = worker(store, t)
    lead, _ = sent_lead(store, w, t, email="sara@dune.example", key="b1")
    deals.request_payment(store, w, lead["id"], amount=625, now=NOW)
    board = deals.money_board(store)
    assert "Deposits requested" in board and "USD 625" in board and f"paid {lead['id']} 625" in board
    assert "On the table: USD 625" in board


def test_no_email_means_no_invoice_but_a_clear_next_step(store):
    live()
    lead, _ = store.upsert_lead(source="x", dedupe_key="n1", company="Acme", channel="call", contact="+12025550111")
    res = deals.request_payment(store, object(), lead["id"])
    assert not res["ok"] and f"paid {lead['id']}" in res["note"]


def test_closer_reads_the_price_on_the_table(store):
    from types import SimpleNamespace
    from nexus.integrations.whatsapp_closer import WhatsAppCloser

    class Router:
        def complete(self, *a, **k):
            return SimpleNamespace(data={"reply": "deal", "intent": "ready_to_pay", "hot": True, "deal_usd": "$2,500"})

    closer = WhatsAppCloser(FakeAlerts(), store=store, router=Router(), voice="", sleep=lambda s: None)
    lead, _ = store.upsert_lead(source="x", dedupe_key="z1", name="Sara", channel="email", contact="s@x.example")
    decision = closer.decide("Them: yes", lead_id=lead["id"], channel="email")
    assert decision["deal_usd"] == 2500 and decision["intent"] == "ready_to_pay"
    closer.apply_outcome(lead, "yes", decision, alert=False)
    assert deals.deal_value(store, store.get_lead(lead["id"])) == 2500


def test_quick_start_offers_are_paid_in_full_up_front(store):
    lead, _ = store.upsert_lead(source="x", dedupe_key="q1", name="Omar", contact="omar@palm.example")
    store.log_event(lead["id"], "message_in", {"text": "ok the $399 one", "deal_usd": 399}, actor="prospect")
    assert deals.deposit_for(store, store.get_lead(lead["id"])) == (399, 399)
    body = deals.payment_email(lead, 399, "USD", "WR-2026-0009", total=399)
    assert "amount: USD 399" in body and "deposit now" not in body


def test_hiring_posts_alert_red_and_urgent(store):
    from nexus.integrations.telegram_alerts import build_alert
    lead, _ = store.upsert_lead(source="reddit_rss", dedupe_key="r:1", name="founder1", channel="reddit",
                                contact="founder1", url="https://www.reddit.com/r/forhire/x",
                                data={"text": "r/forhire: [Hiring] need a dev to finish my lovable app"})
    store.transition(lead["id"], "TRIAGED")
    out = store.stage_outreach(lead["id"], "reddit", "hey, saw your post")
    store.transition(lead["id"], "STAGED", payload={"outreach_id": out["id"]})
    alert = build_alert({"event": "lead.transition", "data": {"to": "STAGED", "lead_id": lead["id"],
                                                              "payload": {"outreach_id": out["id"]}}}, store=store)
    assert alert["priority"] == "red" and alert["text"].startswith("⚡ HIRING POST")
